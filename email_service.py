import smtplib
import os
import socket # Added for network debugging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
import datetime
from invoice_utils import get_isp_company_details_from_db
from flask import render_template_string

# --- CONFIGURATION ---
# Change this to your live domain when deploying
WEB_PORTAL_URL = "https://hudaitsolutions.onrender.com"

def _get_html_template(company_details, title, preheader, body_content):
    """
    Returns a professional, branded HTML template.
    Smarter: Can read keys from saas_settings (for admin) OR company_details (for clients).
    """
    
    # --- Smart key finding ---
    logo_url = company_details.get('saas_logo_url', company_details.get('logo_path'))
    company_name = company_details.get('app_name', company_details.get('company_name', 'Your ISP'))
    company_phone = company_details.get('contact_phone', company_details.get('phone', ''))
    company_email = company_details.get('contact_email', company_details.get('email', ''))
    company_address = company_details.get('contact_address', company_details.get('address', ''))
    
    # Use 'social_media' from saas_settings, or 'social_media_links' from client
    social_links = company_details.get('social_media', company_details.get('social_media_links', {})) 
    if social_links is None:
        social_links = {}
    
    social_html = ""
    # Check for both key formats
    platforms = {
        'facebook_url': 'https://img.icons8.com/color/32/000000/facebook-new.png',
        'youtube_url': 'https://img.icons8.com/color/32/000000/youtube-play.png',
        'linkedin_url': 'https://img.icons8.com/color/32/000000/linkedin.png',
        'social_facebook': 'https://img.icons8.com/color/32/000000/facebook-new.png',
        'social_youtube': 'https://img.icons8.com/color/32/000000/youtube-play.png',
        'social_linkedin': 'https://img.icons8.com/color/32/000000/linkedin.png'
    }
    
    for key, icon in platforms.items():
        url = social_links.get(key)
        if url:
            platform_name = key.split('_')[0].capitalize()
            # Add to social_html and remove the found key to avoid duplicates
            social_html += f'<a href="{url}" style="text-decoration: none; margin: 0 8px;" target="_blank"><img src="{icon}" alt="{platform_name}" style="width: 32px; height: 32px; border: 0;"></a>'
            if key in social_links: social_links.pop(key) 

    # Build Header with a clean white background
    logo_html = ""
    if logo_url:
        logo_html = f"""
        <tr>
            <td style="background-color: #ffffff; padding: 30px 20px 20px 20px; text-align: center;">
                <img src="{logo_url}" alt="{company_name} Logo" style="max-height: 70px; width: auto; border: 0;">
            </td>
        </tr>
        """
    else:
        logo_html = f"""
        <tr>
            <td style="background-color: #ffffff; padding: 20px; text-align: center;">
                <h1 style="color: #5A67D8; margin: 0; font-family: Arial, sans-serif;">{company_name}</h1>
            </td>
        </tr>
        """

    # Build Footer
    footer_content = f"""
    <td style="padding: 30px; text-align: center; color: #888888; font-size: 12px; background-color: #f8faff; border-top: 1px solid #e2e8f0;">
        <div class="social-bar" style="margin-top: 15px; margin-bottom: 15px;"> {social_html} </div>
        <p style="margin: 5px 0; color: #5a657d;">&copy; {datetime.datetime.now().year} {company_name}. All rights reserved.</p>
        <p style="margin: 5px 0; color: #5a657d;">{company_address}</p>
        <p style="margin: 5px 0; color: #5a657d;">{company_phone} | {company_email}</p>
        <p style="color: #aaa; margin-top: 10px;">Powered by Huda IT Solutions</p>
    </td>
    """

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device-width, initial-scale=1.0"> <title>{title}</title> </head>
    <body style="margin: 0; padding: 0; background-color: #f8faff; width: 100%;" bgcolor="#f8faff">
        <div style="display:none;font-size:1px;color:#f8faff;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
            {preheader}
        </div>
        <table width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#f8faff" style="width: 100%; background-color: #f8faff; padding: 20px 0;">
            <tr> <td align="center">
                <table width="600" border="0" cellpadding="0" cellspacing="0" style="width: 100%; max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; font-family: Arial, sans-serif; border: 1px solid #e2e8f0; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
                    
                    {logo_html}
                    
                    <tr><td style="padding: 30px 40px; color: #2d3748; line-height: 1.7; font-size: 16px;">
                        {body_content}
                    </td></tr>
                    
                    <tr>{footer_content}</tr>
                </table>
            </td> </tr>
        </table>
    </body> </html>
    """


def _send_email(company_details, to_email, subject, html_body, attachment_path=None):
    """
    Sends an email using the configured SMTP server.
    Supports both Port 587 (TLS) and Port 465 (SSL).
    Fallbacks to OS Environment Variables if company_details are missing keys.
    """
    
    # 1. Try fetching from the Dictionary (DB)
    smtp_host = company_details.get('smtp_host')
    smtp_port_val = company_details.get('smtp_port')
    smtp_user = company_details.get('smtp_user')
    smtp_pass = company_details.get('smtp_pass')
    
    # 2. Fallback to OS Environment Variables (Render Settings) if missing
    if not smtp_host:
        smtp_host = os.environ.get('SMTP_HOST')
    if not smtp_port_val:
        smtp_port_val = os.environ.get('SMTP_PORT', 587) # Default to 587
    if not smtp_user:
        smtp_user = os.environ.get('SMTP_USER')
    if not smtp_pass:
        smtp_pass = os.environ.get('SMTP_PASSWORD')

    # Convert port to int safely
    try:
        smtp_port = int(smtp_port_val)
    except:
        smtp_port = 587

    # --- AUTO-CORRECT GMAIL PORT ---
    # Render blocks port 587. If we see Gmail + 587, force switch to 465.
    if "smtp.gmail.com" in str(smtp_host) and smtp_port == 587:
        print("NOTICE: Auto-switching Gmail to Port 465 (SSL) for cloud compatibility.")
        smtp_port = 465
    # -------------------------------

    # --- Smart sender_name logic ---
    sender_name = company_details.get('sender_name', 
                    company_details.get('app_name', 'ISP Support'))
    
    from_email = smtp_user 

    if not all([smtp_host, smtp_port, smtp_user, smtp_pass]):
        print("Email Error: SMTP settings are incomplete. Cannot send email.")
        return False, "SMTP settings are not configured."
        
    try:
        msg = MIMEMultipart('mixed')
        
        msg['From'] = formataddr((str(sender_name), from_email))
        msg['To'] = to_email
        msg['Subject'] = str(subject)
        
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, 'rb') as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
            encoders.encode_base64(part)
            
            part.add_header(
                'Content-Disposition',
                f'attachment; filename="{os.path.basename(attachment_path)}"',
            )
            
            msg.attach(part)
        
        # --- NEW CONNECTION LOGIC (SSL VS TLS) ---
        print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
        
        if smtp_port == 465:
            # Use SSL directly (Fixes Errno 101 on Render)
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            # Use Standard TLS
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.starttls() 

        with server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            print(f"Email successfully sent to {to_email}")
            
        return True, "Email sent successfully."

    except Exception as e:
        print(f"Email Sending Error: {e}")
        # Detailed debugging if it fails
        try:
            print(f"DNS Resolution for {smtp_host}: {socket.gethostbyname(smtp_host)}")
        except:
            print("DNS Resolution Failed")
        return False, str(e)

def send_invoice_email(customer_email, customer_name, invoice_data, company_details, pdf_attachment_path):
    """Generates and sends an invoice receipt email."""
    
    subject = f"Payment Receipt for Invoice #{invoice_data['invoice_number']}"
    preheader = f"Thank you for your payment of {invoice_data['amount']:.2f} BDT."
    
    greeting = f"Hi {customer_name},"
    
    body_content = f"""
    <h2 style="color: #2d3748; margin-top: 0;">Thank You for Your Payment!</h2>
    <p>{greeting}</p>
    <p>We have successfully received <b>{invoice_data['amount']:.2f} BDT</b>.</p>
    <p>Your payment receipt for invoice <b>{invoice_data['invoice_number']}</b> is attached to this email for your records.</p>
    <p>We appreciate your business.</p>
    <p style="margin-bottom: 0;">Thank you,<br/>The {company_details.get("sender_name", "Team")}</p>
    """
    
    html_body = _get_html_template(company_details, subject, preheader, body_content)
    return _send_email(company_details, customer_email, subject, html_body, pdf_attachment_path)

def send_ticket_status_update_email(customer_email, customer_name, ticket_number, ticket_subject, new_status, company_details, ticket_id=None):
    """Notifies a customer when their ticket status is updated (e.g., Resolved)."""
    
    status_text = new_status.lower()
    color = "#38A169" if new_status == "Resolved" else "#6c757d" 
    
    title = f"Your Support Ticket #{ticket_number} has been {new_status}"
    preheader = f"An update on your support ticket: {ticket_subject}"
    
    body = f"""
    <h2 style="color: #2d3748; margin-top: 0;">Ticket Status Updated</h2>
    <p style="margin-bottom: 20px;">Dear {customer_name},</p>
    <p style="margin-bottom: 20px;">This is a notification that the status of your support ticket has been updated.</p>
    
    <div style="background-color: #f8faff; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <p style="margin: 0 0 10px 0; color: #5a657d;"><b>Ticket:</b> #{ticket_number}</p>
        <p style="margin: 0 0 10px 0; color: #5a657d;"><b>Subject:</b> {ticket_subject}</p>
        <p style="margin: 0; color: #2d3748;"><b>New Status:</b> <span style="color: {color}; font-weight: bold;">{new_status}</span></p>
    </div>
    """
    
    if new_status == "Resolved":
        if ticket_id:
            rating_link = f"{WEB_PORTAL_URL}/ticket/{ticket_id}/feedback"
            body += f"""
            <div style="text-align: center; margin: 30px 0;">
                <p style="margin-bottom: 15px; font-weight: bold; color: #333;">How did we do?</p>
                <a href="{rating_link}" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; font-size: 16px;">
                    Rate Our Support
                </a>
                <p style="font-size: 12px; color: #777; margin-top: 10px;">Or click: <a href="{rating_link}">{rating_link}</a></p>
            </div>
            """
        body += f'<p style="margin-top: 20px;">Our team believes your issue has been resolved. If you continue to experience problems, please feel free to open a new one.</p>'
    
    elif new_status == "Closed":
        body += f'<p style="margin-top: 20px;">This ticket is now considered closed.</p>'
        
    body += f'<p style="margin-top: 20px; margin-bottom: 0;">Thank you,<br>The {company_details.get("sender_name", "Team")}</p>'
    
    html_body = _get_html_template(company_details, title, preheader, body)
    return _send_email(company_details, customer_email, title, html_body, attachment_path=None)

# --- NEW: Employee Notification for Assigned Ticket ---
def send_ticket_assignment_email(employee_email, employee_name, ticket_number, customer, ticket_description, company_details=None):
    """
    Notifies an employee when a new ticket is assigned to them.
    """
    if not company_details:
        # Fallback to defaults if not passed, although caller usually passes it.
        company_details = {
            'sender_name': 'ISP System',
            'app_name': 'ISP Manager'
        }

    title = f"New Ticket Assigned: #{ticket_number}"
    preheader = f"You have a new support ticket assigned via Auto-Assign."

    cust_name = customer.get('full_name', 'N/A') if customer else 'N/A'
    cust_phone = customer.get('phone_number', 'N/A') if customer else 'N/A'
    cust_addr = customer.get('address', 'N/A') if customer else 'N/A'

    body = f"""
    <h2 style="color: #2d3748; margin-top: 0;">New Ticket Assigned</h2>
    <p>Hello {employee_name},</p>
    <p>A new support ticket has been assigned to you via the <b>Auto-Assign (Zone Logic)</b> system.</p>

    <div style="background-color: #f8faff; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: 20px;">
        <p style="margin: 5px 0;"><b>Ticket #:</b> {ticket_number}</p>
        <p style="margin: 5px 0;"><b>Customer:</b> {cust_name}</p>
        <p style="margin: 5px 0;"><b>Phone:</b> {cust_phone}</p>
        <p style="margin: 5px 0;"><b>Address:</b> {cust_addr}</p>
    </div>

    <h3 style="color: #2d3748; font-size: 16px;">Issue Description:</h3>
    <div style="background-color: #eee; padding: 15px; border-radius: 5px; color: #333; font-style: italic;">
        "{ticket_description}"
    </div>

    <p style="margin-top: 20px;">Please check your Employee Portal for full details and to update the status.</p>
    """
    
    html_body = _get_html_template(company_details, title, preheader, body)
    return _send_email(company_details, employee_email, title, html_body)

def send_generic_email(saas_settings, to_email, subject, html_body):
    try:
        success, message = _send_email(
            company_details=saas_settings, 
            to_email=to_email, 
            subject=subject, 
            html_body=html_body, 
            attachment_path=None
        )
        if not success:
            raise Exception(message)
    except Exception as e:
        print(f"Error in send_generic_email wrapper: {e}")
        raise e 

def send_order_confirmation_email(saas_settings, to_email, company_name, order_number, plan_snapshot, track_url=None, pay_now_url=None, payment_details=None):
    plan_name = plan_snapshot.get('name', 'N/A')
    title = f"Your Order ({order_number}) Has Been Placed!"
    preheader = f"Thank you for your order for the {plan_name} plan. Your order number is {order_number}."
    
    payment_html = ""
    button_html = ""

    if payment_details:
        final_price = plan_snapshot.get('final_price', 'N/A')
        title = f"Payment Received! Order ({order_number})" 
        
        payment_html = f"""
        <h3 style="color: #2d3748; margin-top: 25px; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">Payment Details (Cash Memo)</h3>
        <table width="100%" cellpadding="0" cellspacing="0" style="width: 100%; color: #5a657d;">
            <tr>
                <td style="padding: 5px 0;">Payment Status:</td>
                <td style="padding: 5px 0; text-align: right; color: #38A169; font-weight: bold;">PAID</td>
            </tr>
            <tr>
                <td style="padding: 5px 0;">Amount Paid:</td>
                <td style="padding: 5px 0; text-align: right; font-weight: bold; color: #2d3748;">{final_price} BDT</td>
            </tr>
            <tr>
                <td style="padding: 5px 0;">Payment Method:</td>
                <td style="padding: 5px 0; text-align: right;">{payment_details.get('method', 'N/A')}</td>
            </tr>
            <tr>
                <td style="padding: 5px 0;">Bank TxID:</td>
                <td style="padding: 5px 0; text-align: right;">{payment_details.get('bank_trx_id', 'N/A')}</td>
            </tr>
        </table>
        <p style="margin-top: 25px; text-align: center;">You can track the status of your order at any time using the button below:</p>
        """
        button_html = f"""
        <p style="text-align: center; margin-top: 20px; margin-bottom: 25px;">
            <a href="{track_url}" style="background-color: #5A67D8; color: white; padding: 12px 25px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px;">
                Track Your Order Status
            </a>
        </p>
        """
    
    elif pay_now_url:
        payment_html = """
        <p style="margin-top: 25px; text-align: center;">
            Your order is now pending. Our team will contact you shortly, or you can
            complete your payment now using our secure gateway:
        </p>
        """
        button_html = f"""
        <p style="text-align: center; margin-top: 20px; margin-bottom: 25px;">
            <a href="{pay_now_url}" style="background-color: #38A169; color: white; padding: 12px 25px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px;">
                Pay Now
            </a>
        </p>
        """
    
    else:
        payment_html = """
        <p style="margin-top: 25px; text-align: center;">
            Your order is pending. You can track its status or complete your payment
            using the button below:
        </p>
        """
        button_html = f"""
        <p style="text-align: center; margin-top: 20px; margin-bottom: 25px;">
            <a href="{track_url}" style="background-color: #5A67D8; color: white; padding: 12px 25px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px;">
                Track / Pay for Order
            </a>
        </p>
        """
    
    body_content = f"""
    <h2 style="color: #2d3748; margin-top: 0;">Thank You for Your Order, {company_name}!</h2>
    <p>We have successfully received your order for the <b>{plan_name}</b> plan. Your order number is below.</p>
    
    <div style="background-color: #f8faff; padding: 20px; border-radius: 8px; font-family: 'Courier New', Courier, monospace; border: 1px solid #e2e8f0; text-align: center;">
        <span style="font-size: 16px; color: #5a657d;">Your Order Number is:</span><br>
        <span style="font-size: 28px; font-weight: 700; color: #5A67D8; letter-spacing: 2px;">{order_number}</span>
    </div>
    
    {payment_html}
    {button_html}
    
    <p style="margin-bottom: 0;">We will notify you by email as soon as your account is approved and activated.</p>
    """
    
    html_body = _get_html_template(saas_settings, title, preheader, body_content)
    return _send_email(saas_settings, to_email, title, html_body, attachment_path=None)

def send_product_order_confirmation_customer(saas_settings, to_email, customer_name, order_number, order_items, total_amount, shipping_cost, payment_details=None):
    items_html = ""
    subtotal_before_discount = 0.0
    subtotal_after_discount = 0.0

    for item in order_items:
        price = float(item.get('final_price_per_item', item.get('price_per_item', 0)))
        qty = int(item.get('quantity', 1))
        item_subtotal = price * qty
        subtotal_after_discount += item_subtotal

        original_price = float(item.get('original_price', price)) 
        item_original_total = original_price * qty
        subtotal_before_discount += item_original_total
        
        items_html += f"""
        <tr>
            <td style="padding: 10px; border: 1px solid #e2e8f0;">{item.get('name', 'N/A')} (Qty: {qty})</td>
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">
                {item_subtotal:.2f} BDT
            </td>
        </tr>
        """
    
    total_discount = subtotal_before_discount - subtotal_after_discount

    payment_html = ""
    if payment_details:
        title = f"Payment Received! Order ({order_number})"
        preheader = "Thank you for your payment. Your order is being processed."
        payment_html = f"""
        <h3 style="color: #2d3748; margin-top: 25px; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">Payment Details (Cash Memo)</h3>
        <table width="100%" cellpadding="0" cellspacing="0" style="width: 100%; color: #5a657d;">
            <tr>
                <td style="padding: 5px 0;">Payment Status:</td>
                <td style="padding: 5px 0; text-align: right; color: #38A169; font-weight: bold;">PAID</td>
            </tr>
            <tr>
                <td style="padding: 5px 0;">Payment Method:</td>
                <td style="padding: 5px 0; text-align: right;">{payment_details.get('method', 'N/A')}</td>
            </tr>
            <tr>
                <td style="padding: 5px 0;">Bank TxID:</td>
                <td style="padding: 5px 0; text-align: right;">{payment_details.get('bank_trx_id', 'N/A')}</td>
            </tr>
        </table>
        <p style="margin-top: 20px;">Our team will pack your items and ship them to you shortly. You will receive another notification once your order is shipped.</p>
        """
    else:
        title = f"Your Order ({order_number}) Has Been Placed!"
        preheader = "Your Cash on Delivery order is being processed."
        payment_html = f"""
        <h3 style="color: #2d3748; margin-top: 25px; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">Payment Details</h3>
        <table width="100%" cellpadding="0" cellspacing="0" style="width: 100%; color: #5a657d;">
            <tr>
                <td style="padding: 5px 0;">Payment Status:</td>
                <td style="padding: 5px 0; text-align: right; color: #D97706; font-weight: bold;">Pending (Cash on Delivery)</td>
            </tr>
            <tr>
                <td style="padding: 5px 0;">Amount Due:</td>
                <td style="padding: 5px 0; text-align: right; font-weight: bold; color: #2d3748;">{total_amount:.2f} BDT</td>
            </tr>
        </table>
        <p style="margin-top: 20px;">Our team will pack your items and a delivery agent will contact you. Please have the exact amount ready.</p>
        """

    body_content = f"""
    <h2 style="color: #2d3748; margin-top: 0;">Thank You for Your Order, {customer_name}!</h2>
    <p>We have successfully received your product order. Your order number is below.</p>
    
    <div style="background-color: #f8faff; padding: 20px; border-radius: 8px; font-family: 'Courier New', Courier, monospace; border: 1px solid #e2e8f0; text-align: center;">
        <span style="font-size: 16px; color: #5a657d;">Your Order Number is:</span><br>
        <span style="font-size: 28px; font-weight: 700; color: #5A67D8; letter-spacing: 2px;">{order_number}</span>
    </div>

    <h3 style="color: #2d3748; margin-top: 25px; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">Order Summary</h3>
    <table width="100%" cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: collapse; color: #2d3748;">
        <tr style="background-color: #f8faff; font-weight: bold;">
            <td style="padding: 12px; border: 1px solid #e2e8f0;">Item</td>
            <td style="padding: 12px; border: 1px solid #e2e8f0; text-align: right;">Price</td>
        </tr>
        {items_html}
        
        <tr style="font-weight: 500;">
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">Subtotal (Original):</td>
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">{subtotal_before_discount:.2f} BDT</td>
        </tr>
        <tr style="font-weight: 500; color: #E53E3E;">
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">Discount:</td>
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">-{total_discount:.2f} BDT</td>
        </tr>
        <tr style="font-weight: 500;">
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">Subtotal (After Discount):</td>
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">{subtotal_after_discount:.2f} BDT</td>
        </tr>
        <tr style="font-weight: 500;">
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">Shipping:</td>
            <td style="padding: 10px; border: 1px solid #e2e8f0; text-align: right;">{shipping_cost:.2f} BDT</td>
        </tr>
        <tr style="background-color: #f8faff; font-weight: bold; font-size: 1.1em;">
            <td style="padding: 12px; border: 1px solid #e2e8f0; text-align: right;">Total Amount</td>
            <td style="padding: 12px; border: 1px solid #e2e8f0; text-align: right;">{total_amount:.2f} BDT</td>
        </tr>
        </table>
    
    {payment_html}
    
    <p style="margin-bottom: 0;">Thank you!</p>
    """
    
    html_body = _get_html_template(saas_settings, title, preheader, body_content)
    return _send_email(saas_settings, to_email, title, html_body, attachment_path=None)

def send_service_reactivated_email(customer_email, customer_name, company_id):
    company_details = get_isp_company_details_from_db(company_id)
    
    title = "Service Reactivated - Welcome Back!"
    preheader = "Your internet service has been restored."
    
    body = f"""
    <p style="margin-bottom: 20px; color: #333333;">Dear {customer_name},</p>
    <p style="margin-bottom: 20px; color: #333333;">Great news! We have received your payment, and your internet service has been <b>successfully reactivated</b>.</p>
    
    <div style="background-color: #d4edda; padding: 20px; border-radius: 5px; border: 1px solid #c3e6cb; color: #155724;">
        <h4 style="margin: 0 0 10px 0;">Account Status: Active</h4>
        <p style="margin: 0;">You are back online!</p>
    </div>
    
    <p style="margin-top: 20px; color: #333333;">If your internet does not work immediately, please try <b>restarting your router</b> (power it off for 10 seconds, then on again).</p>
    
    <p style="margin-bottom: 0;">Thank you for staying with us,<br>The {company_details.get("sender_name", "Team")}</p>
    """
    
    html_body = _get_html_template(company_details, title, preheader, body)
    return _send_email(company_details, customer_email, title, html_body)
