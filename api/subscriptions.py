"""Subscription endpoints: /tiers, /create, /current, /cancel, /pause, /resume."""
import os
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
import logging

from database import get_db
from models import User, SubscriptionTier, UserSubscription
from auth import get_current_user
from api.payments import CreatePaymentOut
from services.subscriptions import (
    get_subscription_tier,
    create_razorpay_subscription,
    create_user_subscription,
    cancel_subscription,
    pause_subscription,
    resume_subscription,
    get_user_subscription,
    get_credit_breakdown as get_credit_breakdown_service,
    upgrade_subscription,
    addon_subscription,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# ── Schemas ──────────────────────────────────────────────────
class SubscriptionTierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tier_name: str
    display_name: str
    price_paise: int
    base_credits: int
    bonus_percent: int
    total_credits: int
    billing_period: str
    is_active: bool


class CreateSubscriptionIn(BaseModel):
    tier_id: int = Field(gt=0)


class SubscriptionOut(BaseModel):
    subscription_id: str
    tier_name: str
    status: str
    next_billing_date: str
    current_period_end: str
    subscription_credits_balance: int
    price_paise: int
    razorpay_subscription_id: str | None = None
    razorpay_key_id: str | None = None


class SubscriptionActionOut(BaseModel):
    status: str
    message: str


class VerifySubscriptionIn(BaseModel):
    razorpay_subscription_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class CreditBreakdownOut(BaseModel):
    subscription_credits: int
    subscription_tier: str | None = None
    subscription_status: str | None = None  # active | pending
    topup_credits: int
    total: int
    next_billing_date: str | None = None


# ── Endpoints ────────────────────────────────────────────────
@router.get("/tiers", response_model=list[SubscriptionTierOut])
def get_subscription_tiers(db: Session = Depends(get_db)) -> list[SubscriptionTierOut]:
    """Get all active subscription tiers."""
    tiers = db.query(SubscriptionTier).filter(SubscriptionTier.is_active == True).all()
    return [SubscriptionTierOut.model_validate(tier) for tier in tiers]


@router.post("/create", response_model=SubscriptionOut)
def create_subscription(
    req: CreateSubscriptionIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    """Create a new subscription (pending payment verification)."""
    try:
        # Get tier
        tier = get_subscription_tier(db, req.tier_id)

        # Check if user already has active/pending subscription
        existing = db.query(UserSubscription).filter(
            UserSubscription.user_id == current_user.id,
            UserSubscription.status.in_(["active", "pending"]),
        ).first()

        if existing and existing.status == "pending":
            db.delete(existing)
            db.commit()
            existing = None

        if existing:
            # If the requested tier is higher (more expensive) than current subscription, treat as upgrade
            if tier.price_paise > existing.tier.price_paise:
                subscription = upgrade_subscription(db, current_user, tier)
                return SubscriptionOut(
                    subscription_id=subscription.id,
                    tier_name=tier.display_name,
                    status=subscription.status,
                    next_billing_date=subscription.next_billing_date.isoformat(),
                    current_period_end=subscription.current_period_end.isoformat(),
                    subscription_credits_balance=subscription.subscription_credits_balance,
                    price_paise=tier.price_paise,
                )
            elif tier.price_paise == existing.tier.price_paise:
                # Same tier: treat as addon (extends billing period by 30 days + adds credits)
                subscription = addon_subscription(db, current_user, tier)
                return SubscriptionOut(
                    subscription_id=subscription.id,
                    tier_name=existing.tier.display_name,
                    status=subscription.status,
                    next_billing_date=subscription.next_billing_date.isoformat(),
                    current_period_end=subscription.current_period_end.isoformat(),
                    subscription_credits_balance=subscription.subscription_credits_balance,
                    price_paise=existing.tier.price_paise,
                )
            else:
                # Lower tier: keep current tier active, but grant credits of the lower tier
                existing.subscription_credits_balance += tier.total_credits
                
                # Update user.credits
                current_user.credits = existing.subscription_credits_balance + current_user.topup_credits_balance

                # Log transaction
                from models import CreditTransaction
                txn = CreditTransaction(
                    user_id=current_user.id,
                    kind="subscription_addon",
                    amount=tier.total_credits,
                    balance_after=current_user.credits,
                    note=f"Granted {tier.display_name} credits to existing {existing.tier.display_name} subscription",
                )
                db.add(existing)
                db.add(current_user)
                db.add(txn)
                db.commit()
                db.refresh(existing)
                
                return SubscriptionOut(
                    subscription_id=existing.id,
                    tier_name=existing.tier.display_name,
                    status=existing.status,
                    next_billing_date=existing.next_billing_date.isoformat(),
                    current_period_end=existing.current_period_end.isoformat(),
                    subscription_credits_balance=existing.subscription_credits_balance,
                    price_paise=existing.tier.price_paise,
                )

        # Create Razorpay subscription
        razorpay_subscription_id, razorpay_plan_id = create_razorpay_subscription(db, current_user, tier)

        # Create DB record
        subscription = create_user_subscription(
            db,
            current_user,
            tier,
            razorpay_subscription_id,
            razorpay_plan_id,
        )

        return SubscriptionOut(
            subscription_id=subscription.id,
            tier_name=tier.display_name,
            status=subscription.status,
            next_billing_date=subscription.next_billing_date.isoformat(),
            current_period_end=subscription.current_period_end.isoformat(),
            subscription_credits_balance=subscription.subscription_credits_balance,
            price_paise=tier.price_paise,
            razorpay_subscription_id=subscription.razorpay_subscription_id,
            razorpay_key_id=os.getenv("RAZORPAY_KEY_ID"),
        )

    except HTTPException as e:
        raise e
    except ValueError as e:
        logger.warning(f"Subscription creation failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Subscription creation error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create subscription",
        )


@router.get("/current", response_model=SubscriptionOut)
def get_current_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    """Get current active subscription for user."""
    subscription_data = get_user_subscription(db, current_user.id)

    if not subscription_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription found",
        )

    return SubscriptionOut(**subscription_data)


@router.get("/breakdown", response_model=CreditBreakdownOut)
def get_credit_breakdown(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreditBreakdownOut:
    """Get credit balance breakdown (subscription + topup + admin)."""
    breakdown = get_credit_breakdown_service(db, current_user)
    return CreditBreakdownOut(**breakdown)


@router.post("/verify", response_model=SubscriptionActionOut)
def verify_subscription(
    req: VerifySubscriptionIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionActionOut:
    """Verify subscription payment signature and activate it immediately."""
    import hmac
    import hashlib

    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Razorpay secret not configured",
        )

    # Verify signature
    data = f"{req.razorpay_payment_id}|{req.razorpay_subscription_id}"
    expected_signature = hmac.new(
        key_secret.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, req.razorpay_signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payment signature",
        )

    # Get user subscription record
    subscription = db.query(UserSubscription).filter(
        UserSubscription.razorpay_subscription_id == req.razorpay_subscription_id,
        UserSubscription.user_id == current_user.id
    ).first()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription record not found",
        )

    # Activate subscription
    if subscription.status == "pending":
        subscription.status = "active"
        tier = subscription.tier
        subscription.subscription_credits_balance = tier.total_credits

        # Update user.credits
        current_user.credits = subscription.subscription_credits_balance + current_user.topup_credits_balance

        # Log credit transaction
        from models import CreditTransaction
        txn = CreditTransaction(
            user_id=current_user.id,
            kind="subscription_grant",
            amount=tier.total_credits,
            balance_after=current_user.credits,
            note=f"Subscription verified: {tier.display_name}",
        )
        db.add(subscription)
        db.add(current_user)
        db.add(txn)
        db.commit()

    return SubscriptionActionOut(
        status="active",
        message="Subscription successfully verified and activated",
    )


@router.post("/cancel", response_model=SubscriptionActionOut)
def cancel_current_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionActionOut:
    """Cancel current subscription (effective end of current period)."""
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == current_user.id,
        UserSubscription.status.in_(["active", "pending"]),
    ).first()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active or pending subscription to cancel",
        )

    try:
        cancel_subscription(current_user.id)
        return SubscriptionActionOut(
            status="canceled",
            message=f"Subscription canceled. Access until {subscription.current_period_end.date()}.",
        )
    except Exception as e:
        logger.error(f"Failed to cancel subscription: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel subscription",
        )


@router.post("/pause", response_model=SubscriptionActionOut)
def pause_current_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionActionOut:
    """Pause subscription (don't renew, but keep access until period end)."""
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == current_user.id,
        UserSubscription.status == "active",
    ).first()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription to pause",
        )

    try:
        pause_subscription(current_user.id)
        return SubscriptionActionOut(
            status="paused",
            message=f"Subscription paused. Access until {subscription.current_period_end.date()}.",
        )
    except Exception as e:
        logger.error(f"Failed to pause subscription: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to pause subscription",
        )


@router.post("/resume", response_model=SubscriptionActionOut)
def resume_current_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionActionOut:
    """Resume a paused subscription."""
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == current_user.id,
        UserSubscription.status == "paused",
    ).first()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No paused subscription to resume",
        )

    try:
        resume_subscription(current_user.id)
        return SubscriptionActionOut(
            status="active",
            message=f"Subscription resumed. Next billing: {subscription.next_billing_date.date()}.",
        )
    except Exception as e:
        logger.error(f"Failed to resume subscription: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume subscription",
        )


@router.post("/upgrade", response_model=CreatePaymentOut)
def upgrade_to_tier(
    req: CreateSubscriptionIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreatePaymentOut:
    """Upgrade to higher tier: create Razorpay order for tier price difference.

    Only credits on webhook verification (subscription.charged).
    """
    try:
        new_tier = get_subscription_tier(db, req.tier_id)
        old_subscription = db.query(UserSubscription).filter(
            UserSubscription.user_id == current_user.id,
            UserSubscription.status == "active",
        ).first()

        if not old_subscription:
            raise ValueError("No active subscription to upgrade")

        old_tier = old_subscription.tier

        if new_tier.price_paise <= old_tier.price_paise:
            raise ValueError("Can only upgrade to higher tier")

        # Create Razorpay order for upgrade amount
        upgrade_amount = new_tier.price_paise - old_tier.price_paise
        from services.razorpay import create_order

        order_data = create_order(
            user_id=current_user.id,
            amount_paise=upgrade_amount,
            credits=0,  # Credits credited only after webhook
        )

        # Mark old subscription as pending upgrade
        old_subscription.status = "pending_upgrade"
        db.add(old_subscription)
        db.commit()

        return CreatePaymentOut(**order_data)

    except ValueError as e:
        logger.warning(f"Upgrade failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Upgrade error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create upgrade order",
        )


@router.post("/addon", response_model=CreatePaymentOut)
def addon_to_current_tier(
    req: CreateSubscriptionIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreatePaymentOut:
    """Add another month: create Razorpay order for tier price.

    Only extends cycle & credits on webhook verification.
    """
    try:
        tier = get_subscription_tier(db, req.tier_id)
        subscription = db.query(UserSubscription).filter(
            UserSubscription.user_id == current_user.id,
            UserSubscription.status == "active",
        ).first()

        if not subscription:
            raise ValueError("No active subscription")

        if tier.id != subscription.tier.id:
            raise ValueError("Addon must match current tier")

        # Create Razorpay order for addon
        from services.razorpay import create_order

        order_data = create_order(
            user_id=current_user.id,
            amount_paise=tier.price_paise,
            credits=0,  # Credits credited only after webhook
        )

        # Mark as pending addon
        subscription.status = "pending_addon"
        db.add(subscription)
        db.commit()

        return CreatePaymentOut(**order_data)

    except ValueError as e:
        logger.warning(f"Addon failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Addon error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create addon order",
        )
