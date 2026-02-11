import os
import requests
import base64
import datetime
from invoice_utils import get_isp_company_details_from_db

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
    Sends email via Brevo API (Port 443 - Never Blocked).
    Requires 'BREVO_API_KEY' and 'SENDER_EMAIL' in environment variables.
    """
    api_key = os.environ.get('BREVO_API_KEY')
    sender_email = os.environ.get('SENDER_EMAIL') 
    
    if not api_key or not sender_email:
        print("BREVO SETUP ERROR: Please set BREVO_API_KEY and SENDER_EMAIL in Render Environment.")
        return False, "Brevo configuration missing."

    url = "https://api.brevo.com/v3/smtp/email"
    
    # Calculate Sender Name
    sender_name = company_details.get('sender_name', company_details.get('app_name', 'ISP Portal'))
    
    # Build Payload
    payload = {
        "sender": {
            "name": sender_name,
            "email": sender_email
        },
        "to": [
            {"email": to_email}
        ],
        "subject": str(subject),
        "htmlContent": html_body
    }

    # Add Reply-To (So customers reply to the ISP, not the system email)
    company_contact = company_details.get('contact_email')
    if company_contact:
        payload['replyTo'] = {"email": company_contact}

    # Handle Attachment
    if attachment_path and os.path.exists(attachment_path):
        try:
            with open(attachment_path, "rb") as f:
                encoded_content = base64.b64encode(f.read()).decode('utf-8')
                
            payload['attachment'] = [{
                "name": os.path.basename(attachment_path),
                "content": encoded_content
            }]
        except Exception as e:
            print(f"Attachment Error: {e}")

    # Send Request
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if response.status_code in [200, 201, 202]:
            print(f"Brevo Success: Email sent to {to_email}")
            return True, "Email sent successfully."
        else:
            print(f"Brevo Failed: {response.status_code} - {response.text}")
            return False, f"Brevo Error: {response.text}"
            
    except Exception as e:
        print(f"Brevo Connection Error: {e}")
        return False, str(e)

# --- Wrapper Functions (PRESERVED EXACTLY AS BEFORE) ---

def send_invoice_email(customer_email, customer_name, invoice_data, company_details, pdf_attachment_path):
    subject = f"Payment Receipt for Invoice #{invoice_data['invoice_number']}"
    preheader = f"Thank you for your payment of {invoice_data['amount']:.2f} BDT."
    greeting = f"Hi {customer_name},"
    
    body_content = f"""
    <h2 style="color: #2d3748; margin-top: 0;">Thank You for Your Payment!</h2>
    <p>{greeting}</p>
    <p>We have successfully received <b>{invoice_data['amount']:.2f} BDT</b>.</p>
    <p>Your payment receipt for invoice <b>{invoice_data['invoice_number']}</b> is attached to this email.</p>
    <p>We appreciate your business.</p>
    <p style="margin-bottom: 0;">Thank you,<br/>The {company_details.get("sender_name", "Team")}</p>
    """
    
    html_body = _get_html_template(company_details, subject, preheader, body_content)
    return _send_email(company_details, customer_email, subject, html_body, pdf_attachment_path)

def send_ticket_status_update_email(customer_email, customer_name, ticket_number, ticket_subject, new_status, company_details, ticket_id=None):
    color = "#38A169" if new_status == "Resolved" else "#6c757d"
    title = f"Your Support Ticket #{ticket_number} has been {new_status}"
    preheader = f"An update on your support ticket: {ticket_subject}"
    
    body = f"""
    <h2 style="color: #2d3748; margin-top: 0;">Ticket Status Updated</h2>
    <p>Dear {customer_name},</p>
    <p>This is a notification that the status of your support ticket has been updated.</p>
    <div style="background-color: #f8faff; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <p style="margin: 0 0 10px 0;"><b>Ticket:</b> #{ticket_number}</p>
        <p style="margin: 0 0 10px 0;"><b>Subject:</b> {ticket_subject}</p>
        <p style="margin: 0;"><b>New Status:</b> <span style="color: {color}; font-weight: bold;">{new_status}</span></p>
    </div>
    """
    
    if new_status == "Resolved" and ticket_id:
        link = f"{WEB_PORTAL_URL}/ticket/{ticket_id}/feedback"
        body += f"""
        <div style="text-align: center; margin: 30px 0;">
            <a href="{link}" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">Rate Our Support</a>
        </div>
        """
        
    body += f'<p style="margin-top: 20px;">Thank you,<br>The {company_details.get("sender_name", "Team")}</p>'
    html_body = _get_html_template(company_details, title, preheader, body)
    return _send_email(company_details, customer_email, title, html_body)

def send_ticket_assignment_email(employee_email, employee_name, ticket_number, customer, ticket_description, company_details=None):
    if not company_details: company_details = {'sender_name': 'ISP System', 'app_name': 'ISP Manager'}
    
    title = f"New Ticket Assigned: #{ticket_number}"
    preheader = "You have a new support ticket assigned via Auto-Assign."
    cust_name = customer.get('full_name', 'N/A')
    
    body = f"""
    <h2 style="color: #2d3748;">New Ticket Assigned</h2>
    <p>Hello {employee_name},</p>
    <p>A new support ticket has been assigned to you.</p>
    <div style="background-color: #f8faff; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <p><b>Ticket #:</b> {ticket_number}</p>
        <p><b>Customer:</b> {cust_name}</p>
    </div>
    <h3>Issue:</h3>
    <div style="background-color: #eee; padding: 15px; border-radius: 5px;">"{ticket_description}"</div>
    """
    html_body = _get_html_template(company_details, title, preheader, body)
    return _send_email(company_details, employee_email, title, html_body)

def send_generic_email(saas_settings, to_email, subject, html_body):
    return _send_email(saas_settings, to_email, subject, html_body)

def send_order_confirmation_email(saas_settings, to_email, company_name, order_number, plan_snapshot, track_url=None, pay_now_url=None, payment_details=None):
    plan_name = plan_snapshot.get('name', 'N/A')
    title = f"Order #{order_number} Received"
    preheader = f"Order for {plan_name} plan."
    
    body = f"""
    <h2 style="color: #2d3748;">Order Received</h2>
    <p>Thank you for ordering the <b>{plan_name}</b> plan for {company_name}.</p>
    <p><b>Order Number:</b> {order_number}</p>
    """
    
    if track_url:
        body += f'<p><a href="{track_url}" style="background-color: #5A67D8; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Track Order</a></p>'
    
    html_body = _get_html_template(saas_settings, title, preheader, body)
    return _send_email(saas_settings, to_email, title, html_body)

def send_product_order_confirmation_customer(saas_settings, to_email, customer_name, order_number, order_items, total_amount, shipping_cost, payment_details=None):
    title = f"Order #{order_number} Confirmed"
    preheader = "Your product order has been received."
    
    body = f"""
    <h2 style="color: #2d3748;">Order Confirmed</h2>
    <p>Hi {customer_name},</p>
    <p>We have received your order <b>#{order_number}</b>.</p>
    <p><b>Total:</b> {total_amount} BDT</p>
    <p>We will notify you when it ships.</p>
    """
    html_body = _get_html_template(saas_settings, title, preheader, body)
    return _send_email(saas_settings, to_email, title, html_body)

def send_service_reactivated_email(customer_email, customer_name, company_id):
    company_details = get_isp_company_details_from_db(company_id)
    title = "Service Reactivated"
    preheader = "Your internet service is back online."
    
    body = f"""
    <p>Dear {customer_name},</p>
    <p>Good news! Your internet service has been <b>successfully reactivated</b>.</p>
    <p>If it doesn't work immediately, please restart your router.</p>
    """
    html_body = _get_html_template(company_details, title, preheader, body)
    return _send_email(company_details, customer_email, title, html_body)
