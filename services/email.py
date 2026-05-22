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
SMTP_SENDER = os.getenv("SMTP_SENDER", SMTP_USERNAME or "no-reply@clipforge.com").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_SENDER = os.getenv("RESEND_SENDER", "").strip()

def send_verification_email(email: str, name: str, token: str):
    verification_link = f"{FRONTEND_URL}/verify-email?token={token}"
    subject = "Verify your email for ClipForge"
    
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0d0e12; color: #e2e8f0; padding: 30px; margin: 0;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #161920; border: 1px solid #2d3139; border-radius: 16px; padding: 32px; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);">
          <div style="text-align: center; margin-bottom: 24px;">
            <span style="font-size: 24px; font-weight: 900; color: #a78bfa; letter-spacing: 0.5px;">ClipForge</span>
          </div>
          <h2 style="font-size: 20px; font-weight: 700; margin-top: 0; margin-bottom: 8px; color: #ffffff;">Verify your email address</h2>
          <p style="font-size: 14px; line-height: 20px; color: #94a3b8; margin-bottom: 24px;">
            Hi {name},<br><br>
            Welcome to ClipForge! Please verify your email address to unlock your account and start creating viral clips.
          </p>
          <div style="text-align: center; margin-bottom: 24px;">
            <a href="{verification_link}" style="display: inline-block; background-color: #7c3aed; color: #ffffff; font-weight: 600; font-size: 14px; text-decoration: none; padding: 12px 28px; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(124, 58, 237, 0.4);">
              Verify Email Address
            </a>
          </div>
          <p style="font-size: 12px; line-height: 18px; color: #64748b; margin-bottom: 16px;">
            Or copy and paste this link into your browser:
            <br>
            <a href="{verification_link}" style="color: #a78bfa; text-decoration: underline;">{verification_link}</a>
          </p>
          <hr style="border: 0; border-top: 1px solid #2d3139; margin-bottom: 16px;">
          <p style="font-size: 11px; line-height: 16px; color: #475569; margin: 0; text-align: center;">
            This link will expire in 24 hours. If you did not sign up for this account, you can safely ignore this email.
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
            if not resend_sender or "@gmail.com" in resend_sender.lower() or "no-reply@clipforge.com" in resend_sender.lower():
                resend_sender = "ClipForge <onboarding@resend.dev>"

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
                raise e
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
        raise e
