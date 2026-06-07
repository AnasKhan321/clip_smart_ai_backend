"""Subscription tier management and renewal logic."""
import os
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import razorpay

from database import SessionLocal
from models import SubscriptionTier, UserSubscription, User, CreditTransaction

logger = logging.getLogger(__name__)


def calculate_credits(base_credits: int, bonus_percent: int) -> int:
    """Calculate total credits = base + (base * bonus / 100)."""
    return base_credits + int(base_credits * bonus_percent / 100)


def get_subscription_tier(db: Session, tier_id: int) -> SubscriptionTier:
    """Fetch tier config by ID."""
    tier = db.query(SubscriptionTier).filter(SubscriptionTier.id == tier_id).first()
    if not tier:
        raise ValueError(f"Tier {tier_id} not found")
    return tier


def get_razorpay_client() -> razorpay.Client:
    """Get Razorpay client instance."""
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise ValueError("RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET not set")
    return razorpay.Client(auth=(key_id, key_secret))


def create_razorpay_subscription(
    db: Session, user: User, tier: SubscriptionTier
) -> tuple[str, str]:
    """Create subscription in Razorpay: Plan (with inline item) → Subscription.

    Returns: (subscription_id, plan_id)
    """
    client = get_razorpay_client()

    try:
        # Step 1: Create Plan with inline item object
        plan_data = {
            "period": "monthly",
            "interval": 1,
            "item": {
                "name": tier.display_name,
                "amount": tier.price_paise,
                "currency": "INR",
            },
        }
        logger.info(f"Creating Razorpay plan for {tier.display_name}")
        plan = client.plan.create(data=plan_data)
        plan_id = plan["id"]
        logger.info(f"✓ Created Razorpay plan {plan_id}")

        # Step 2: Create Subscription using the plan
        sub_data = {
            "plan_id": plan_id,
            "customer_notify": 1,
            "quantity": 1,
            "total_count": 12,
        }
        logger.info(f"Creating Razorpay subscription with plan {plan_id}")
        subscription = client.subscription.create(data=sub_data)
        logger.info(f"✓ Created Razorpay subscription {subscription['id']}")
        return subscription["id"], plan_id

    except Exception as e:
        logger.error(f"Razorpay API error: {str(e)}")
        raise Exception(f"Failed to create subscription: {str(e)}")


def create_user_subscription(
    db: Session,
    user: User,
    tier: SubscriptionTier,
    razorpay_subscription_id: str,
    razorpay_plan_id: str,
) -> UserSubscription:
    """Create UserSubscription record in DB (pending webhook verification).

    Credits will be granted only after subscription.charged webhook is received
    and verified for security.
    """
    now = datetime.utcnow()
    # Start pending, will become active after first webhook
    subscription = UserSubscription(
        user_id=user.id,
        subscription_tier_id=tier.id,
        razorpay_subscription_id=razorpay_subscription_id,
        razorpay_plan_id=razorpay_plan_id,
        status="pending",  # Changed from active
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        next_billing_date=now + timedelta(days=30),
        subscription_credits_balance=0,  # Changed from tier.total_credits
        subscription_credits_used=0,
    )
    db.add(subscription)

    user.subscription_tier_id = tier.id
    db.add(user)
    db.commit()
    db.refresh(subscription)

    logger.info(f"Created pending user subscription {subscription.id} for user {user.id} (awaiting webhook)")
    return subscription


def handle_subscription_renewal(razorpay_subscription_id: str) -> None:
    """Called when subscription.charged webhook fires.

    Grant monthly credits, update billing dates.
    """
    db = SessionLocal()
    try:
        subscription = db.query(UserSubscription).filter(
            UserSubscription.razorpay_subscription_id == razorpay_subscription_id
        ).first()

        if not subscription:
            logger.warning(f"Subscription {razorpay_subscription_id} not found in DB")
            return

        user = subscription.user
        tier = subscription.tier

        # Grant credits
        subscription.subscription_credits_balance = tier.total_credits
        subscription.current_period_start = datetime.utcnow()
        subscription.current_period_end = datetime.utcnow() + timedelta(days=30)
        subscription.next_billing_date = datetime.utcnow() + timedelta(days=30)

        # Update user.credits
        user.credits = subscription.subscription_credits_balance + user.topup_credits_balance

        txn = CreditTransaction(
            user_id=user.id,
            kind="subscription_grant",
            amount=tier.total_credits,
            balance_after=user.credits,
            note=f"Monthly renewal: {tier.display_name}",
        )

        db.add(subscription)
        db.add(user)
        db.add(txn)
        db.commit()

        logger.info(f"Renewed subscription {razorpay_subscription_id}, granted {tier.total_credits} credits")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to handle subscription renewal: {str(e)}")
    finally:
        db.close()


def cancel_subscription(user_id: str) -> None:
    """Mark subscription as canceled (no renewal after current period)."""
    db = SessionLocal()
    try:
        subscription = db.query(UserSubscription).filter(
            UserSubscription.user_id == user_id,
            UserSubscription.status.in_(["active", "pending"]),
        ).first()

        if not subscription:
            logger.warning(f"No active or pending subscription found for user {user_id}")
            return

        subscription.status = "canceled"
        subscription.is_renewing = False
        subscription.canceled_at = datetime.utcnow()

        db.add(subscription)
        db.commit()

        logger.info(f"Canceled subscription {subscription.id} for user {user_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to cancel subscription: {str(e)}")
    finally:
        db.close()


def pause_subscription(user_id: str) -> None:
    """Pause subscription (don't renew, but keep access until period end)."""
    db = SessionLocal()
    try:
        subscription = db.query(UserSubscription).filter(
            UserSubscription.user_id == user_id,
            UserSubscription.status == "active",
        ).first()

        if not subscription:
            logger.warning(f"No active subscription found for user {user_id}")
            return

        subscription.status = "paused"
        subscription.is_renewing = False

        db.add(subscription)
        db.commit()

        logger.info(f"Paused subscription {subscription.id} for user {user_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to pause subscription: {str(e)}")
    finally:
        db.close()


def resume_subscription(user_id: str) -> None:
    """Resume a paused subscription."""
    db = SessionLocal()
    try:
        subscription = db.query(UserSubscription).filter(
            UserSubscription.user_id == user_id,
            UserSubscription.status == "paused",
        ).first()

        if not subscription:
            logger.warning(f"No paused subscription found for user {user_id}")
            return

        subscription.status = "active"
        subscription.is_renewing = True

        db.add(subscription)
        db.commit()

        logger.info(f"Resumed subscription {subscription.id} for user {user_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to resume subscription: {str(e)}")
    finally:
        db.close()


def get_user_subscription(db: Session, user_id: str) -> dict:
    """Get current subscription for user (if active or pending)."""
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == user_id,
        (UserSubscription.status.in_(["active", "pending"])) |
        (UserSubscription.status.in_(["canceled", "paused"]) & (UserSubscription.current_period_end > datetime.utcnow()))
    ).order_by(UserSubscription.created_at.desc()).first()

    if not subscription:
        return None

    tier = subscription.tier

    return {
        "subscription_id": subscription.id,
        "tier_id": tier.id,
        "tier_name": tier.display_name,
        "status": subscription.status,
        "next_billing_date": subscription.next_billing_date.isoformat(),
        "current_period_end": subscription.current_period_end.isoformat(),
        "subscription_credits_balance": subscription.subscription_credits_balance,
        "price_paise": tier.price_paise,
        "razorpay_subscription_id": subscription.razorpay_subscription_id,
        "razorpay_key_id": os.getenv("RAZORPAY_KEY_ID"),
    }


def get_credit_breakdown(db: Session, user: User) -> dict:
    """Get credit balance breakdown (subscription + topup + admin)."""
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == user.id,
        (UserSubscription.status.in_(["active", "pending"])) |
        (UserSubscription.status.in_(["canceled", "paused"]) & (UserSubscription.current_period_end > datetime.utcnow()))
    ).order_by(UserSubscription.created_at.desc()).first()

    sub_credits = subscription.subscription_credits_balance if subscription else 0
    topup_credits = user.topup_credits_balance

    return {
        "subscription_credits": sub_credits,
        "subscription_tier": subscription.tier.display_name if subscription else None,
        "topup_credits": topup_credits,
        "total": sub_credits + topup_credits,
        "next_billing_date": subscription.next_billing_date.isoformat() if subscription else None,
        "subscription_status": subscription.status if subscription else None,
    }


def calculate_proration(
    db: Session,
    current_tier: SubscriptionTier,
    new_tier: SubscriptionTier,
    current_period_end: datetime,
) -> dict:
    """Calculate proration adjustment for mid-cycle tier change.

    Returns: { credit_adjustment: int, refund_paise: int }
    """
    now = datetime.utcnow()
    days_remaining = (current_period_end - now).days + 1
    days_in_period = (current_period_end - (current_period_end - timedelta(days=30))).days

    # Daily rate for each tier
    daily_cost_current = current_tier.price_paise / days_in_period
    daily_cost_new = new_tier.price_paise / days_in_period

    # Remaining charge for current tier
    remaining_charge = daily_cost_current * days_remaining

    # What they should have paid for new tier
    new_tier_charge = daily_cost_new * days_remaining

    # Adjust payment/credit
    credit_difference = new_tier.total_credits - current_tier.total_credits
    credit_adjustment = int(credit_difference * days_remaining / days_in_period)

    # If upgrading (new tier > current tier), user doesn't get refund, just more credits
    # If downgrading, we might owe them a refund
    refund_paise = 0
    if new_tier.price_paise < current_tier.price_paise:
        refund_paise = int(remaining_charge - new_tier_charge)

    return {
        "credit_adjustment": credit_adjustment,
        "refund_paise": refund_paise,
        "days_remaining": days_remaining,
    }


def upgrade_subscription(
    db: Session,
    user: User,
    new_tier: SubscriptionTier,
) -> UserSubscription:
    """Upgrade subscription: cancel old cycle, start new one with full credits.

    Old unused credits are kept, new tier starts immediately.
    """
    old_subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == user.id,
        UserSubscription.status == "active",
    ).first()

    if not old_subscription:
        raise ValueError("No active subscription to upgrade")

    old_tier = old_subscription.tier

    if new_tier.id == old_tier.id:
        raise ValueError("Same tier, use addon instead")

    if new_tier.price_paise < old_tier.price_paise:
        raise ValueError("Can only upgrade to higher tier, not downgrade")

    # Keep old unused subscription credits
    old_unused_credits = old_subscription.subscription_credits_balance

    # Cancel old subscription
    old_subscription.status = "canceled"
    old_subscription.canceled_at = datetime.utcnow()

    # Create new subscription starting now
    now = datetime.utcnow()
    new_subscription = UserSubscription(
        user_id=user.id,
        subscription_tier_id=new_tier.id,
        razorpay_subscription_id=f"upgraded_{user.id}_{now.timestamp()}",
        razorpay_plan_id="upgraded",
        status="active",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        next_billing_date=now + timedelta(days=30),
        subscription_credits_balance=new_tier.total_credits + old_unused_credits,
        subscription_credits_used=0,
    )

    # Update user.credits
    user.credits = (new_tier.total_credits + old_unused_credits) + user.topup_credits_balance

    # Log upgrade transaction
    txn = CreditTransaction(
        user_id=user.id,
        kind="subscription_upgrade",
        amount=new_tier.total_credits,
        balance_after=user.credits,
        note=f"Upgraded from {old_tier.display_name} to {new_tier.display_name} + {old_unused_credits} old credits",
    )

    user.subscription_tier_id = new_tier.id
    db.add(user)
    db.add(old_subscription)
    db.add(new_subscription)
    db.add(txn)
    db.commit()
    db.refresh(new_subscription)

    logger.info(
        f"Subscription upgraded {new_subscription.id}: {old_tier.display_name} → {new_tier.display_name}, "
        f"new credits: {new_tier.total_credits}, old credits kept: {old_unused_credits}"
    )

    return new_subscription


def addon_subscription(
    db: Session,
    user: User,
    tier: SubscriptionTier,
) -> UserSubscription:
    """Add another subscription month: extend cycle + add full credits.

    Same tier purchase extends billing period by 30 days and adds full credits.
    """
    subscription = db.query(UserSubscription).filter(
        UserSubscription.user_id == user.id,
        UserSubscription.status == "active",
    ).first()

    if not subscription:
        raise ValueError("No active subscription")

    current_tier = subscription.tier

    if tier.id != current_tier.id:
        raise ValueError("Addon must match current tier. Use upgrade for tier changes.")

    # Extend cycle by 30 days
    subscription.current_period_end = subscription.current_period_end + timedelta(days=30)
    subscription.next_billing_date = subscription.next_billing_date + timedelta(days=30)

    # Add full tier credits
    subscription.subscription_credits_balance += tier.total_credits

    # Update user.credits
    user.credits = subscription.subscription_credits_balance + user.topup_credits_balance

    # Log addon transaction
    txn = CreditTransaction(
        user_id=user.id,
        kind="subscription_addon",
        amount=tier.total_credits,
        balance_after=user.credits,
        note=f"Added {tier.display_name} subscription extension",
    )

    db.add(subscription)
    db.add(user)
    db.add(txn)
    db.commit()
    db.refresh(subscription)

    logger.info(
        f"Subscription addon {subscription.id}: extended to {subscription.next_billing_date.date()}, "
        f"added {tier.total_credits} credits"
    )

    return subscription


def seed_subscription_tiers(db: Session) -> None:
    """Seed 4 subscription tiers from CREDIT_PRICE_INR env var (idempotent)."""
    # Get credit price from env
    credit_price_inr = float(os.getenv("CREDIT_PRICE_INR", "99.99"))
    credit_price_paise = int(credit_price_inr * 100)

    tiers_config = [
        {
            "tier_name": "starter",
            "display_name": "Starter",
            "price_paise": 99900,  # ₹999
            "bonus_percent": 10,
        },
        {
            "tier_name": "pro",
            "display_name": "Pro",
            "price_paise": 199900,  # ₹1,999
            "bonus_percent": 15,
        },
        {
            "tier_name": "professional",
            "display_name": "Professional",
            "price_paise": 299900,  # ₹2,999
            "bonus_percent": 20,
        },
        {
            "tier_name": "enterprise",
            "display_name": "Enterprise",
            "price_paise": 999900,  # ₹9,999
            "bonus_percent": 50,
        },
    ]

    for config in tiers_config:
        existing = db.query(SubscriptionTier).filter(
            SubscriptionTier.tier_name == config["tier_name"]
        ).first()

        if existing:
            continue

        # Calculate base credits from price
        base_credits = config["price_paise"] // credit_price_paise
        total_credits = calculate_credits(base_credits, config["bonus_percent"])

        tier = SubscriptionTier(
            tier_name=config["tier_name"],
            display_name=config["display_name"],
            price_paise=config["price_paise"],
            base_credits=base_credits,
            bonus_percent=config["bonus_percent"],
            total_credits=total_credits,
            billing_period="monthly",
            is_active=True,
        )

        db.add(tier)
        logger.info(f"Seeded tier: {config['tier_name']} ({base_credits} base + {config['bonus_percent']}% = {total_credits} total credits)")

    db.commit()
