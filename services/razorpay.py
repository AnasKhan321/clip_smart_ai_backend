import os
import hmac
import hashlib
import json
import logging
from datetime import datetime
import razorpay
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Payment, User, CreditTransaction

logger = logging.getLogger(__name__)

_client = None


def get_razorpay_client() -> razorpay.Client:
    global _client
    if not _client:
        key_id = os.getenv("RAZORPAY_KEY_ID")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise ValueError("RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET not set")
        _client = razorpay.Client(auth=(key_id, key_secret))
    return _client


def create_order(user_id: str, amount_paise: int, credits: int) -> dict:
    """Create Razorpay order. Returns order data with order_id, key_id."""
    client = get_razorpay_client()

    # Receipt must be <= 40 chars; use timestamp hash
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    receipt = f"ord_{timestamp % 1000000}"  # Max 14 chars

    order_data = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
    }

    try:
        razorpay_order = client.order.create(data=order_data)
        logger.info(f"Created order: {razorpay_order['id']} for user {user_id}")

        # Save order to DB (pending)
        db = SessionLocal()
        try:
            payment = Payment(
                user_id=user_id,
                razorpay_order_id=razorpay_order["id"],
                amount_paise=amount_paise,
                credits_granted=credits,
                status="pending",
            )
            db.add(payment)
            db.commit()
            db.refresh(payment)
            logger.info(f"Saved payment record: {payment.id}")
        finally:
            db.close()

        return {
            "order_id": razorpay_order["id"],
            "amount": amount_paise,
            "currency": "INR",
            "key_id": os.getenv("RAZORPAY_KEY_ID"),
        }
    except Exception as e:
        logger.error(f"Failed to create Razorpay order: {str(e)}")
        raise


def verify_payment(razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str, db: Session | None = None) -> dict:
    """Verify payment signature. If valid, credit user and return success."""
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    # Verify signature (use constant-time comparison)
    data = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected_signature = hmac.new(
        key_secret.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, razorpay_signature):
        logger.error(f"Signature mismatch for order {razorpay_order_id}")
        raise ValueError("Invalid payment signature")

    # Get payment record
    is_local_session = False
    if db is None:
        db = SessionLocal()
        is_local_session = True
    try:
        payment = db.query(Payment).filter(
            Payment.razorpay_order_id == razorpay_order_id
        ).first()

        if not payment:
            logger.error(f"Payment record not found for order {razorpay_order_id}")
            raise ValueError("Payment record not found")

        if payment.status == "success":
            logger.warning(f"Payment already verified: {razorpay_order_id}")
            return {"status": "success", "payment_id": payment.id}

        # Update payment with signature
        payment.razorpay_payment_id = razorpay_payment_id
        payment.razorpay_signature = razorpay_signature
        payment.status = "success"
        payment.verified_at = datetime.utcnow()

        # Credit user
        user = db.query(User).filter(User.id == payment.user_id).first()
        if not user:
            logger.error(f"User not found: {payment.user_id}")
            raise ValueError("User not found")

        if payment.payment_type == "topup":
            user.topup_credits_balance += payment.credits_granted

        # Recalculate user.credits
        from models import UserSubscription
        subscription = db.query(UserSubscription).filter(
            UserSubscription.user_id == user.id,
            (UserSubscription.status.in_(["active", "pending"])) |
            (UserSubscription.status.in_(["canceled", "paused"]) & (UserSubscription.current_period_end > datetime.utcnow()))
        ).order_by(UserSubscription.created_at.desc()).first()
        user.credits = (subscription.subscription_credits_balance if subscription else 0) + user.topup_credits_balance

        # Log transaction
        txn = CreditTransaction(
            user_id=payment.user_id,
            kind="payment",
            amount=payment.credits_granted,
            balance_after=user.credits,
            note=f"Payment {razorpay_payment_id}",
        )

        db.add(txn)
        if is_local_session:
            db.commit()
            db.refresh(payment)

        logger.info(f"Payment verified & credited {payment.credits_granted} credits to {payment.user_id}")
        return {"status": "success", "payment_id": payment.id}

    except Exception as e:
        if is_local_session:
            db.rollback()
        logger.error(f"Failed to verify payment: {str(e)}")
        raise
    finally:
        if is_local_session:
            db.close()


def mark_payment_failed(razorpay_order_id: str) -> None:
    """Mark order as failed (user canceled/payment failed)."""
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter(
            Payment.razorpay_order_id == razorpay_order_id
        ).first()

        if payment and payment.status == "pending":
            payment.status = "failed"
            db.commit()
            logger.info(f"Marked payment {razorpay_order_id} as failed")
    except Exception as e:
        logger.error(f"Failed to mark payment as failed: {str(e)}")
    finally:
        db.close()


def handle_webhook(payload: str, signature: str) -> None:
    """Verify & process Razorpay webhook."""
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    expected_signature = hmac.new(
        key_secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        logger.error("Webhook signature mismatch")
        raise ValueError("Invalid webhook signature")

    event = json.loads(payload)
    event_type = event.get("event")

    logger.info(f"Processing webhook: {event_type}")

    if event_type == "payment.authorized":
        payment_data = event.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_data.get("order_id")
        razorpay_payment_id = payment_data.get("id")

        # Mark as success (actual amount verification happens in verify_payment)
        db = SessionLocal()
        try:
            payment = db.query(Payment).filter(
                Payment.razorpay_order_id == razorpay_order_id
            ).first()
            if payment and payment.status == "pending":
                payment.razorpay_payment_id = razorpay_payment_id
                payment.status = "success"
                payment.verified_at = datetime.utcnow()

                user = db.query(User).filter(User.id == payment.user_id).first()
                if payment.payment_type == "topup":
                    user.topup_credits_balance += payment.credits_granted

                # Recalculate user.credits
                from models import UserSubscription
                subscription = db.query(UserSubscription).filter(
                    UserSubscription.user_id == user.id,
                    (UserSubscription.status.in_(["active", "pending"])) |
                    (UserSubscription.status.in_(["canceled", "paused"]) & (UserSubscription.current_period_end > datetime.utcnow()))
                ).order_by(UserSubscription.created_at.desc()).first()
                user.credits = (subscription.subscription_credits_balance if subscription else 0) + user.topup_credits_balance

                txn = CreditTransaction(
                    user_id=payment.user_id,
                    kind="payment",
                    amount=payment.credits_granted,
                    balance_after=user.credits,
                    note=f"Payment {razorpay_payment_id}",
                )
                db.add(txn)
                db.commit()
                logger.info(f"Webhook: credited {payment.credits_granted} to {payment.user_id}")
        except Exception as e:
            db.rollback()
            logger.error(f"Webhook processing failed: {str(e)}")
        finally:
            db.close()

    elif event_type == "payment.failed":
        payment_data = event.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_data.get("order_id")
        mark_payment_failed(razorpay_order_id)


def handle_subscription_webhook(payload: str, signature: str) -> None:
    """Verify & process Razorpay subscription webhook."""
    from models import UserSubscription

    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    expected_signature = hmac.new(
        key_secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        logger.error("Subscription webhook signature mismatch")
        raise ValueError("Invalid webhook signature")

    event = json.loads(payload)
    event_type = event.get("event")

    logger.info(f"Processing subscription webhook: {event_type}")

    db = SessionLocal()
    try:
        if event_type == "subscription.charged":
            sub_data = event.get("payload", {}).get("subscription", {}).get("entity", {})
            razorpay_subscription_id = sub_data.get("id")
            charged_amount = sub_data.get("amount")

            subscription = db.query(UserSubscription).filter(
                UserSubscription.razorpay_subscription_id == razorpay_subscription_id
            ).first()

            if subscription:
                tier = subscription.tier
                user = subscription.user

                # Verify amount matches tier price (security check)
                if charged_amount != tier.price_paise:
                    logger.error(
                        f"Amount mismatch for subscription {razorpay_subscription_id}: "
                        f"expected {tier.price_paise}, got {charged_amount}"
                    )
                    subscription.status = "past_due"
                    db.add(subscription)
                    db.commit()
                    return

                # Grant credits
                subscription.subscription_credits_balance = tier.total_credits
                if subscription.status in ("pending", "past_due"):
                    subscription.status = "active"  # Activate on first successful charge or recovery
                from datetime import timedelta as td
                subscription.current_period_start = datetime.utcnow()
                subscription.current_period_end = datetime.utcnow() + td(days=30)
                subscription.next_billing_date = datetime.utcnow() + td(days=30)

                # Update user.credits
                user.credits = subscription.subscription_credits_balance + user.topup_credits_balance

                # Convert all free clips to paid (subscription active, preserve clips)
                from models import Clip, Job
                free_clips = db.query(Clip).join(Job).filter(
                    Job.user_id == user.id,
                    Clip.credit_type == "free"
                ).all()
                for clip in free_clips:
                    clip.credit_type = "paid"

                txn = CreditTransaction(
                    user_id=user.id,
                    kind="subscription_grant",
                    amount=tier.total_credits,
                    balance_after=user.credits,
                    note=f"Subscription charged: {tier.display_name}",
                )

                db.add(subscription)
                db.add(txn)
                db.commit()
                logger.info(f"Subscription {razorpay_subscription_id} charged & activated, granted {tier.total_credits} credits. {len(free_clips)} clips preserved.")

        elif event_type == "subscription.completed":
            sub_data = event.get("payload", {}).get("subscription", {}).get("entity", {})
            razorpay_subscription_id = sub_data.get("id")

            subscription = db.query(UserSubscription).filter(
                UserSubscription.razorpay_subscription_id == razorpay_subscription_id
            ).first()

            if subscription:
                subscription.status = "completed"
                db.add(subscription)
                db.commit()
                logger.info(f"Subscription {razorpay_subscription_id} completed")

        elif event_type == "subscription.authentication_failed":
            sub_data = event.get("payload", {}).get("subscription", {}).get("entity", {})
            razorpay_subscription_id = sub_data.get("id")

            subscription = db.query(UserSubscription).filter(
                UserSubscription.razorpay_subscription_id == razorpay_subscription_id
            ).first()

            if subscription:
                subscription.status = "past_due"
                user = subscription.user
                tier = subscription.tier
                db.add(subscription)
                db.commit()
                logger.warning(f"Subscription {razorpay_subscription_id} payment failed")

                # Send email notification
                try:
                    from services.email import send_payment_failed_email
                    send_payment_failed_email(
                        email=user.email,
                        name=user.name or user.email.split("@")[0],
                        subscription_tier=tier.display_name,
                        reason="authentication_failed",
                    )
                except Exception as e:
                    logger.error(f"Failed to send payment failure email: {e}")

    except Exception as e:
        db.rollback()
        logger.error(f"Subscription webhook processing failed: {str(e)}")
    finally:
        db.close()
