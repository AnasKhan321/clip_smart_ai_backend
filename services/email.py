import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

# Config
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_SENDER = os.getenv("SMTP_SENDER", SMTP_USERNAME or "noreply@vibeclip.in").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_SENDER = os.getenv("RESEND_SENDER", "").strip()

def send_verification_email(email: str, name: str, token: str):
    verification_link = f"{FRONTEND_URL}/verify-email?token={token}"
    subject = "Verify your email for VibeClip"

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0a0f14; color: #e2e8f0; padding: 30px; margin: 0;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #0d1520; border: 1px solid #1a2d3d; border-radius: 16px; padding: 32px; box-shadow: 0 10px 40px -5px rgba(6, 182, 212, 0.08);">

          <!-- Logo -->
          <div style="text-align: center; margin-bottom: 28px;">
            <span style="font-size: 26px; font-weight: 900; background: linear-gradient(135deg, #22d3ee 0%, #06b6d4 50%, #0891b2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; letter-spacing: 0.5px;">VibeClip</span>
          </div>

          <!-- Divider -->
          <div style="height: 1px; background: linear-gradient(90deg, transparent, #06b6d4, transparent); margin-bottom: 28px;"></div>

          <h2 style="font-size: 20px; font-weight: 700; margin-top: 0; margin-bottom: 10px; color: #f1f5f9;">Verify your email address</h2>
          <p style="font-size: 14px; line-height: 22px; color: #94a3b8; margin-bottom: 28px;">
            Hi {name},<br><br>
            Welcome to VibeClip! Verify your email address to unlock your account and start creating viral clips.
          </p>

          <!-- CTA Button -->
          <div style="text-align: center; margin-bottom: 28px;">
            <a href="{verification_link}" style="display: inline-block; background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%); color: #ffffff; font-weight: 700; font-size: 14px; text-decoration: none; padding: 13px 32px; border-radius: 10px; box-shadow: 0 4px 20px -2px rgba(6, 182, 212, 0.45); letter-spacing: 0.3px;">
              Verify Email Address
            </a>
          </div>

          <p style="font-size: 12px; line-height: 18px; color: #475569; margin-bottom: 20px;">
            Or copy and paste this link:<br>
            <a href="{verification_link}" style="color: #22d3ee; text-decoration: underline; word-break: break-all;">{verification_link}</a>
          </p>

          <hr style="border: 0; border-top: 1px solid #1a2d3d; margin-bottom: 16px;">
          <p style="font-size: 11px; line-height: 16px; color: #334155; margin: 0; text-align: center;">
            Link expires in 24 hours. If you didn't sign up, ignore this email.
          </p>
        </div>
      </body>
    </html>
    """

    # If Resend API key is configured, send via Resend API (HTTPS - never blocked by cloud hosts)
    if RESEND_API_KEY:
        try:
            import httpx
            resend_sender = RESEND_SENDER or SMTP_SENDER
            if not resend_sender or "@gmail.com" in resend_sender.lower():
                resend_sender = "VibeClip <noreply@vibeclip.in>"

            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": resend_sender,
                    "to": email,
                    "subject": subject,
                    "html": html_content,
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Verification email successfully sent to {email} via Resend API")
                return
            else:
                logger.error(f"Resend API error (status {resp.status_code}): {resp.text}")
                raise Exception(f"Resend error: {resp.text}")
        except Exception as e:
            logger.error(f"Failed to send email via Resend API: {e}")
            if not SMTP_HOST:
                print(f"Resend error. Email verification fallback URL: {verification_link}", flush=True)
                return
            # Else fall through to SMTP fallback below

    # If no SMTP host is configured, log verification link to console (dev fallback)
    if not SMTP_HOST:
        logger.info(
            "\n"
            "=======================================================================\n"
            " DEVELOPMENT EMAIL BYPASS\n"
            " SMTP_HOST is not configured. Logged verification link details below:\n"
            "=======================================================================\n"
            f" To: {email} ({name})\n"
            f" Subject: {subject}\n"
            f" Verification Link: {verification_link}\n"
            "=======================================================================\n"
        )
        # Also print to standard stdout so it's easily visible in running terminal
        print(
            "\n"
            "=======================================================================\n"
            " DEVELOPMENT EMAIL BYPASS\n"
            " SMTP_HOST is not configured. Logged verification link details below:\n"
            "=======================================================================\n"
            f" To: {email} ({name})\n"
            f" Subject: {subject}\n"
            f" Verification Link: {verification_link}\n"
            "=======================================================================\n",
            flush=True
        )
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_SENDER
        msg["To"] = email
        
        # Plain text fallback
        text_content = f"Hi {name},\n\nPlease verify your email address by visiting this link:\n{verification_link}\n\nThis link expires in 24 hours."
        
        msg.attach(MIMEText(text_content, "plain"))
        msg.attach(MIMEText(html_content, "html"))
        
        # Connect to SMTP
        # Port 465 is SSL. Standard ports like 587 or 25 use SMTP then optional STARTTLS
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            if SMTP_USE_TLS:
                server.starttls()
                
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            
        server.sendmail(SMTP_SENDER, email, msg.as_string())
        server.quit()
        logger.info(f"Verification email successfully sent to {email}")
    except Exception as e:
        logger.error(f"Failed to send email to {email} via SMTP: {e}")
        # Log to console so developer is not blocked even if SMTP fails
        print(f"SMTP error. Email verification fallback URL: {verification_link}", flush=True)


def send_payment_failed_email(email: str, name: str, subscription_tier: str, reason: str = "unknown"):
    """Send notification when subscription/payment fails."""
    subject = "Subscription payment failed — Update your payment method"
    billing_link = f"{FRONTEND_URL}/billing"

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0a0f14; color: #e2e8f0; padding: 30px; margin: 0;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #0d1520; border: 1px solid #dc2626; border-radius: 16px; padding: 32px; box-shadow: 0 10px 40px -5px rgba(220, 38, 38, 0.15);">

          <!-- Logo -->
          <div style="text-align: center; margin-bottom: 28px;">
            <span style="font-size: 26px; font-weight: 900; background: linear-gradient(135deg, #22d3ee 0%, #06b6d4 50%, #0891b2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; letter-spacing: 0.5px;">VibeClip</span>
          </div>

          <!-- Alert -->
          <div style="background-color: #7f1d1d; border: 1px solid #dc2626; border-radius: 8px; padding: 16px; margin-bottom: 28px;">
            <h2 style="font-size: 16px; font-weight: 700; color: #fca5a5; margin: 0 0 8px 0;">Payment Failed</h2>
            <p style="font-size: 14px; color: #fca5a5; margin: 0; line-height: 1.5;">
              Your {subscription_tier} subscription payment could not be processed.
            </p>
          </div>

          <p style="font-size: 14px; line-height: 22px; color: #94a3b8; margin-bottom: 28px;">
            Hi {name},<br><br>
            We attempted to renew your subscription but the payment failed. Your subscription may be paused or canceled.
          </p>

          <!-- CTA Button -->
          <div style="text-align: center; margin-bottom: 28px;">
            <a href="{billing_link}" style="display: inline-block; background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%); color: #ffffff; font-weight: 700; font-size: 14px; text-decoration: none; padding: 13px 32px; border-radius: 10px; box-shadow: 0 4px 20px -2px rgba(6, 182, 212, 0.45); letter-spacing: 0.3px;">
              Update Payment Method
            </a>
          </div>

          <hr style="border: 0; border-top: 1px solid #1a2d3d; margin-bottom: 16px;">
          <p style="font-size: 11px; line-height: 16px; color: #334155; margin: 0; text-align: center;">
            If you need help, reply to this email or visit your billing page.
          </p>
        </div>
      </body>
    </html>
    """

    if RESEND_API_KEY:
        try:
            import httpx
            resend_sender = RESEND_SENDER or SMTP_SENDER
            if not resend_sender or "@gmail.com" in resend_sender.lower():
                resend_sender = "VibeClip <noreply@vibeclip.in>"

            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": resend_sender,
                    "to": email,
                    "subject": subject,
                    "html": html_content,
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Payment failure notification sent to {email} via Resend API")
                return
            else:
                logger.error(f"Resend API error (status {resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Failed to send email via Resend API: {e}")
            if not SMTP_HOST:
                return

    if not SMTP_HOST:
        logger.info(f"Payment failure notification for {email} ({subscription_tier}): {reason}")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_SENDER
        msg["To"] = email

        text_content = f"Hi {name},\n\nYour {subscription_tier} subscription payment failed.\n\nPlease update your payment method: {billing_link}"

        msg.attach(MIMEText(text_content, "plain"))
        msg.attach(MIMEText(html_content, "html"))

        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            if SMTP_USE_TLS:
                server.starttls()

        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)

        server.sendmail(SMTP_SENDER, email, msg.as_string())
        server.quit()
        logger.info(f"Payment failure notification sent to {email}")
    except Exception as e:
        logger.error(f"Failed to send payment failure email to {email}: {e}")
