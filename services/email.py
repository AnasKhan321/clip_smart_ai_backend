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


def send_job_completed_email(email: str, name: str, job_id: str, kind: str):
    """Send notification when a job reaches a terminal state.

    kind: "ready" | "no_clips" | "failed"
    """
    review_link = f"{FRONTEND_URL}/review/{job_id}"
    home_link = FRONTEND_URL

    if kind == "ready":
        subject = "Your clips are ready on VibeClip!"
        headline = "Your clips are ready"
        body = "Your video has been processed and your viral clips are ready to review and download."
        cta_label = "View Clips"
        cta_url = review_link
        border_color = "#06b6d4"
        header_color = "#f1f5f9"
    elif kind == "no_clips":
        subject = "VibeClip: No clip-worthy moments found"
        headline = "Processing complete — no clips found"
        body = "Your video was analyzed but no strong clip-worthy moments were detected. Try a different video or regenerate with different settings."
        cta_label = "Try Another Video"
        cta_url = home_link
        border_color = "#f59e0b"
        header_color = "#fef3c7"
    else:  # failed
        subject = "VibeClip: Processing failed"
        headline = "Processing failed"
        body = "Something went wrong while processing your video. Your credits have been refunded. Please try again."
        cta_label = "Try Again"
        cta_url = home_link
        border_color = "#dc2626"
        header_color = "#fca5a5"

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0a0f14; color: #e2e8f0; padding: 30px; margin: 0;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #0d1520; border: 1px solid {border_color}; border-radius: 16px; padding: 32px; box-shadow: 0 10px 40px -5px rgba(6, 182, 212, 0.08);">
          <div style="text-align: center; margin-bottom: 28px;">
            <span style="font-size: 26px; font-weight: 900; background: linear-gradient(135deg, #22d3ee 0%, #06b6d4 50%, #0891b2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; letter-spacing: 0.5px;">VibeClip</span>
          </div>
          <div style="height: 1px; background: linear-gradient(90deg, transparent, {border_color}, transparent); margin-bottom: 28px;"></div>
          <h2 style="font-size: 20px; font-weight: 700; margin-top: 0; margin-bottom: 10px; color: {header_color};">{headline}</h2>
          <p style="font-size: 14px; line-height: 22px; color: #94a3b8; margin-bottom: 28px;">
            Hi {name},<br><br>{body}
          </p>
          <div style="text-align: center; margin-bottom: 28px;">
            <a href="{cta_url}" style="display: inline-block; background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%); color: #ffffff; font-weight: 700; font-size: 14px; text-decoration: none; padding: 13px 32px; border-radius: 10px; box-shadow: 0 4px 20px -2px rgba(6, 182, 212, 0.45); letter-spacing: 0.3px;">{cta_label}</a>
          </div>
          <hr style="border: 0; border-top: 1px solid #1a2d3d; margin-bottom: 16px;">
          <p style="font-size: 11px; line-height: 16px; color: #334155; margin: 0; text-align: center;">
            You're receiving this because you submitted a video on VibeClip.
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
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": resend_sender, "to": email, "subject": subject, "html": html_content},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Job completion email ({kind}) sent to {email} via Resend")
                return
            else:
                logger.error(f"Resend error ({resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Resend job completion email failed: {e}")
            if not SMTP_HOST:
                return

    if not SMTP_HOST:
        logger.info(f"DEV: job completion email ({kind}) for {email} — {cta_url}")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_SENDER
        msg["To"] = email
        msg.attach(MIMEText(f"Hi {name},\n\n{body}\n\n{cta_url}", "plain"))
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
        logger.info(f"Job completion email ({kind}) sent to {email} via SMTP")
    except Exception as e:
        logger.error(f"SMTP job completion email failed for {email}: {e}")


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


def _send_via_resend_or_smtp(to_email: str, subject: str, html_content: str, text_content: str) -> None:
    """Shared send helper — tries Resend API first, falls back to SMTP."""
    if RESEND_API_KEY:
        try:
            import httpx
            sender = RESEND_SENDER or SMTP_SENDER
            if not sender or "@gmail.com" in sender.lower():
                sender = "VibeClip <noreply@vibeclip.in>"
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": sender, "to": to_email, "subject": subject, "html": html_content},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Email '{subject}' sent to {to_email} via Resend")
                return
            logger.error(f"Resend error ({resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Resend send failed: {e}")
            if not SMTP_HOST:
                return

    if not SMTP_HOST:
        logger.info(f"DEV: would send '{subject}' to {to_email}")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_SENDER
        msg["To"] = to_email
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
        server.sendmail(SMTP_SENDER, to_email, msg.as_string())
        server.quit()
        logger.info(f"Email '{subject}' sent to {to_email} via SMTP")
    except Exception as e:
        logger.error(f"SMTP send failed for {to_email}: {e}")


def send_reengagement_email(email: str, name: str) -> None:
    """Send a nudge to users who signed up but haven't submitted any video yet."""
    home_link = FRONTEND_URL
    subject = "Your first clip is waiting — start for free on VibeClip"
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0a0f14; color: #e2e8f0; padding: 30px; margin: 0;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #0d1520; border: 1px solid #1a2d3d; border-radius: 16px; padding: 32px; box-shadow: 0 10px 40px -5px rgba(6, 182, 212, 0.08);">
          <div style="text-align: center; margin-bottom: 28px;">
            <span style="font-size: 26px; font-weight: 900; background: linear-gradient(135deg, #22d3ee 0%, #06b6d4 50%, #0891b2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; letter-spacing: 0.5px;">VibeClip</span>
          </div>
          <div style="height: 1px; background: linear-gradient(90deg, transparent, #06b6d4, transparent); margin-bottom: 28px;"></div>
          <h2 style="font-size: 20px; font-weight: 700; margin-top: 0; margin-bottom: 10px; color: #f1f5f9;">Your clips won't make themselves 🎬</h2>
          <p style="font-size: 14px; line-height: 22px; color: #94a3b8; margin-bottom: 28px;">
            Hi {name},<br><br>
            You signed up for VibeClip yesterday but haven't tried it yet. Paste any YouTube link or upload a video — we'll find the viral moments and cut them into ready-to-post short clips in minutes.
          </p>
          <div style="background-color: #0a1929; border: 1px solid #1a3a5c; border-radius: 10px; padding: 16px; margin-bottom: 28px;">
            <p style="font-size: 13px; color: #64b5f6; font-weight: 600; margin: 0 0 8px 0;">How it works:</p>
            <ol style="font-size: 13px; color: #94a3b8; margin: 0; padding-left: 18px; line-height: 22px;">
              <li>Paste a YouTube URL or upload your video</li>
              <li>AI finds the best viral moments (15–60 sec each)</li>
              <li>Download ready-to-post vertical clips for TikTok, Reels &amp; Shorts</li>
            </ol>
          </div>
          <div style="text-align: center; margin-bottom: 28px;">
            <a href="{home_link}" style="display: inline-block; background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%); color: #ffffff; font-weight: 700; font-size: 14px; text-decoration: none; padding: 13px 32px; border-radius: 10px; box-shadow: 0 4px 20px -2px rgba(6, 182, 212, 0.45); letter-spacing: 0.3px;">
              Make My First Clip
            </a>
          </div>
          <hr style="border: 0; border-top: 1px solid #1a2d3d; margin-bottom: 16px;">
          <p style="font-size: 11px; line-height: 16px; color: #334155; margin: 0; text-align: center;">
            You're receiving this because you created an account on VibeClip.
          </p>
        </div>
      </body>
    </html>
    """
    text_content = (
        f"Hi {name},\n\n"
        "You signed up for VibeClip but haven't created a clip yet.\n\n"
        "Paste any YouTube URL and we'll find the viral moments and cut them into "
        "TikTok/Reels/Shorts-ready clips.\n\n"
        f"Get started: {home_link}\n"
    )
    _send_via_resend_or_smtp(email, subject, html_content, text_content)
