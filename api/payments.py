"""Payment endpoints: /create, /verify, /webhook."""
import os
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from database import get_db
from models import User, Payment
from auth import get_current_user
from services.razorpay import create_order, verify_payment, handle_webhook, mark_payment_failed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


# ── Helpers ──────────────────────────────────────────────────
def _calculate_credits_from_amount(amount_paise: int) -> int:
    """Calculate credits server-side from fixed price-per-credit."""
    credit_price_paise = int(float(os.getenv("CREDIT_PRICE_INR", "99.99")) * 100)
    credits = amount_paise // credit_price_paise
    if credits < 1:
        raise ValueError(f"Amount {amount_paise} paise too small (min {credit_price_paise})")
    return credits


# ── Schemas ──────────────────────────────────────────────────
class CreatePaymentIn(BaseModel):
    amount_paise: int = Field(gt=0, description="Amount in paise (e.g., 50000 for ₹500)")


class CreatePaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    amount: int
    currency: str
    key_id: str


class VerifyPaymentIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class VerifyPaymentOut(BaseModel):
    status: str
    message: str
    payment_id: str


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, serialize_as_any=True)

    id: str
    razorpay_order_id: str
    razorpay_payment_id: str | None = None
    amount_paise: int
    credits_granted: int
    status: str
    payment_type: str = "topup"
    verified_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_db(cls, obj):
        """Convert DB object with datetime fields to schema."""
        data = {
            'id': obj.id,
            'razorpay_order_id': obj.razorpay_order_id,
            'razorpay_payment_id': obj.razorpay_payment_id,
            'amount_paise': obj.amount_paise,
            'credits_granted': obj.credits_granted,
            'status': obj.status,
            'payment_type': obj.payment_type,
            'verified_at': obj.verified_at.isoformat() if obj.verified_at else None,
            'created_at': obj.created_at.isoformat() if obj.created_at else None,
        }
        return cls(**data)


# ── Endpoints ────────────────────────────────────────────────
@router.post("/create", response_model=CreatePaymentOut)
def create_payment(
    req: CreatePaymentIn,
    current_user: User = Depends(get_current_user),
) -> CreatePaymentOut:
    """Create Razorpay order for user. Returns order_id + key_id for frontend checkout."""
    try:
        # Calculate credits server-side from fixed price
        credits = _calculate_credits_from_amount(req.amount_paise)

        order_data = create_order(
            user_id=current_user.id,
            amount_paise=req.amount_paise,
            credits=credits
        )
        return CreatePaymentOut(**order_data)
    except ValueError as e:
        logger.warning(f"Invalid payment request: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to create order: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create payment order"
        )


@router.post("/verify", response_model=VerifyPaymentOut)
def verify_payment_endpoint(
    req: VerifyPaymentIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifyPaymentOut:
    """Verify payment signature & credit user. Called from frontend after Razorpay checkout."""
    try:
        # Verify payment belongs to current user
        payment = db.query(Payment).filter(
            Payment.razorpay_order_id == req.razorpay_order_id
        ).first()

        if not payment or payment.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Payment does not belong to current user"
            )

        # Verify payment
        result = verify_payment(
            razorpay_order_id=req.razorpay_order_id,
            razorpay_payment_id=req.razorpay_payment_id,
            razorpay_signature=req.razorpay_signature,
            db=db,
        )
        db.commit()

        return VerifyPaymentOut(
            status="success",
            message=f"Payment verified and {payment.credits_granted} credits granted",
            payment_id=result["payment_id"]
        )

    except ValueError as e:
        logger.warning(f"Payment verification failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Payment verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment verification failed"
        )


@router.post("/webhook")
async def payment_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
    db: Session = Depends(get_db),
):
    """Razorpay webhook handler. Called by Razorpay after payment."""
    if not x_razorpay_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No signature header"
        )

    try:
        body = await request.body()
        request_body = body.decode('utf-8')
        handle_webhook(payload=request_body, signature=x_razorpay_signature)
        return {"status": "ok"}
    except ValueError as e:
        logger.warning(f"Webhook signature failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook signature"
        )
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed"
        )


@router.get("/status/{order_id}", response_model=PaymentOut)
def get_payment_status(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaymentOut:
    """Get payment status by order_id."""
    payment = db.query(Payment).filter(
        Payment.razorpay_order_id == order_id,
        Payment.user_id == current_user.id
    ).first()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found"
        )

    return PaymentOut.from_db(payment)


@router.get("/history", response_model=list[PaymentOut])
def get_payment_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PaymentOut]:
    """Get all payments for current user."""
    payments = db.query(Payment).filter(
        Payment.user_id == current_user.id
    ).order_by(Payment.created_at.desc()).all()

    return [PaymentOut.from_db(p) for p in payments]


@router.get("/topup/config")
def get_topup_config():
    """Get topup presets from environment."""
    presets_str = os.getenv("TOPUP_PRESETS", "100,500,1000,2000,5000")
    presets = [int(x.strip()) for x in presets_str.split(",")]
    credit_price = float(os.getenv("CREDIT_PRICE_INR", "99.99"))
    min_topup = int(os.getenv("MIN_TOPUP_AMOUNT_INR", "100")) * 100  # Convert to paise
    max_topup = int(os.getenv("MAX_TOPUP_AMOUNT_INR", "99999")) * 100  # Convert to paise

    return {
        "presets": presets,
        "credit_price_inr": credit_price,
        "min_amount_paise": min_topup,
        "max_amount_paise": max_topup,
    }


@router.post("/topup", response_model=CreatePaymentOut)
def create_topup_payment(
    req: CreatePaymentIn,
    current_user: User = Depends(get_current_user),
) -> CreatePaymentOut:
    """Create one-time top-up payment (not subscription)."""
    min_topup = int(os.getenv("MIN_TOPUP_AMOUNT_INR", "100")) * 100
    max_topup = int(os.getenv("MAX_TOPUP_AMOUNT_INR", "99999")) * 100

    if req.amount_paise < min_topup:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Minimum top-up amount is ₹{min_topup // 100}"
        )

    if req.amount_paise > max_topup:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum top-up amount is ₹{max_topup // 100}"
        )

    try:
        # Calculate credits server-side from fixed price
        credits = _calculate_credits_from_amount(req.amount_paise)

        order_data = create_order(
            user_id=current_user.id,
            amount_paise=req.amount_paise,
            credits=credits
        )
        return CreatePaymentOut(**order_data)
    except ValueError as e:
        logger.warning(f"Invalid top-up request: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to create top-up order: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create payment order"
        )


@router.post("/topup/verify", response_model=VerifyPaymentOut)
def verify_topup_payment(
    req: VerifyPaymentIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifyPaymentOut:
    """Verify top-up payment & add to topup_credits_balance."""
    try:
        # Get payment first to validate payment_type
        payment = db.query(Payment).filter(
            Payment.razorpay_order_id == req.razorpay_order_id
        ).first()

        if not payment or payment.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Payment not found or does not belong to user"
            )

        # Validate payment_type to prevent order routing between flows
        if payment.payment_type != "topup":
            raise ValueError(f"Payment is {payment.payment_type}, not topup")

        # Verify payment signature & amount
        result = verify_payment(
            razorpay_order_id=req.razorpay_order_id,
            razorpay_payment_id=req.razorpay_payment_id,
            razorpay_signature=req.razorpay_signature,
            db=db,
        )

        # Convert all free clips to paid (user bought credits, so preserve old clips)
        from models import Clip, Job
        free_clips = db.query(Clip).join(Job).filter(
            Job.user_id == current_user.id,
            Clip.credit_type == "free"
        ).all()
        for clip in free_clips:
            clip.credit_type = "paid"

        db.commit()

        return VerifyPaymentOut(
            status="success",
            message=f"Top-up verified and {payment.credits_granted} credits added. {len(free_clips)} clips preserved.",
            payment_id=result["payment_id"]
        )

    except ValueError as e:
        logger.warning(f"Top-up verification failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Top-up verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Top-up verification failed"
        )


@router.post("/webhooks/subscription")
async def subscription_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
    db: Session = Depends(get_db),
):
    """Razorpay subscription webhook handler. Called by Razorpay on events."""
    if not x_razorpay_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No signature header"
        )

    try:
        body = await request.body()
        request_body = body.decode('utf-8')
        from services.razorpay import handle_subscription_webhook
        handle_subscription_webhook(payload=request_body, signature=x_razorpay_signature)
        return {"status": "ok"}
    except ValueError as e:
        logger.warning(f"Subscription webhook signature failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook signature"
        )
    except Exception as e:
        logger.error(f"Subscription webhook error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed"
        )
