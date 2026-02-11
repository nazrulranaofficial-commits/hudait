import os
import hashlib 
from flask import render_template_string
from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response,jsonify, Response
from flask import send_file
from io import BytesIO
from supabase import create_client, Client
from dotenv import load_dotenv
from supabase_auth.errors import AuthApiError
from postgrest.exceptions import APIError
from datetime import date, datetime, timedelta 
import invoice_utils
from shurjopay_plugin import ShurjopayPlugin, ShurjoPayConfigModel
from types import SimpleNamespace
import logging
import requests
from flask import session, jsonify
import json
from werkzeug.exceptions import abort
from PIL import Image 
import io         
import sys
import email_service 
from database import supabase, get_saas_settings # <-- MODIFIED: Added get_saas_settings
from functools import wraps
from portal_helpers import reactivate_service
from flask_apscheduler import APScheduler
from datetime import date, datetime, timedelta, timezone
from flask import send_from_directory
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor




load_dotenv()
import os
print("--- EMAIL DEBUG INFO ---")
print(f"Host: {os.environ.get('SMTP_HOST')}")
print(f"Port: {os.environ.get('SMTP_PORT')}")
print(f"User: {os.environ.get('SMTP_USER')}")
# Don't print the full password for security, just check length
pwd = os.environ.get('SMTP_PASSWORD', '')
print(f"Password Length: {len(pwd)}") 
print("------------------------")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.debug = os.environ.get("FLASK_DEBUG") == '1'
executor = ThreadPoolExecutor(max_workers=1)
scheduler = APScheduler()

def check_sla_breaches():
    """Background task to mark overdue tickets."""
    with app.app_context():
        try:
            now = datetime.now().isoformat()
            
            # Find open tickets that are past their due date
            response = supabase.table('support_tickets')\
                .select('id, ticket_number, assigned_to_employee_id')\
                .lt('due_at', now)\
                .neq('status', 'Resolved')\
                .neq('status', 'Closed')\
                .neq('status', 'Overdue')\
                .execute()
            
            if response.data:
                for ticket in response.data:
                    print(f"SLA BREACH: Marking Ticket {ticket['ticket_number']} as Overdue.")
                    
                    # 1. Update Status
                    supabase.table('support_tickets').update({'status': 'Overdue'}).eq('id', ticket['id']).execute()
                    
                    # 2. Notify Employee (Optional: Add email logic here)
                    if ticket.get('assigned_to_employee_id'):
                        print(f"--> Alert sent to Employee ID: {ticket['assigned_to_employee_id']}")

        except Exception as e:
            print(f"Scheduler Error: {e}")

# Config for Scheduler
app.config['SCHEDULER_API_ENABLED'] = True
scheduler.init_app(app)
scheduler.start()



# --- ADDED: from_json filter to fix errors in templates ---
def from_json(value):
    """Jinja filter to parse a JSON string."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        print(f"Warning: Could not parse JSON string in template: {value}")
        return {} # Return empty dict on error

app.jinja_env.filters['fromjson'] = from_json
# --- END OF BLOCK ---

REMEMBER_EMAIL_COOKIE = 'last_login_email' 

if not supabase:
    print("CRITICAL: Supabase client not imported from database.py")


def get_user_from_session():
    return session.get('user')

#
# --- *** REPLACE initialize_shurjopay WITH THESE TWO FUNCTIONS *** ---
#

def get_shurjopay_config(saas_settings):
    """
    Helper function to generate the ShurjoPayConfigModel from saas_settings.
    FIXED: Changed 'endpoint' to 'base_url'.
    """
    is_sandbox = saas_settings.get('gateway_sandbox_enabled', True)
    
    if is_sandbox:
        api_endpoint = "https://sandbox.shurjopayment.com"
    else:
        api_endpoint = "https://engine.shurjopayment.com"

    return_url = url_for('shurjopay_return', _external=True)
    cancel_url = url_for('shurjopay_cancel', _external=True)

    return ShurjoPayConfigModel(
        username=saas_settings.get('gateway_store_id'),
        password=saas_settings.get('gateway_store_password'),
        base_url=api_endpoint,  # <--- RENAMED from 'endpoint'
        prefix=saas_settings.get('gateway_prefix'),
        return_url=return_url,
        cancel_url=cancel_url
    )

def initialize_shurjopay(saas_settings, return_url=None, cancel_url=None):
    """
    Initializes and returns a ShurjopayPlugin instance based on saas_settings.
    FIXED: Changed 'endpoint' to 'base_url'.
    """
    is_sandbox = saas_settings.get('gateway_sandbox_enabled', True)
    
    if is_sandbox:
        api_endpoint = "https://sandbox.shurjopayment.com"
    else:
        api_endpoint = "https://engine.shurjopayment.com"

    if not return_url:
        return_url = url_for('shurjopay_return', _external=True)
    if not cancel_url:
        cancel_url = url_for('shurjopay_cancel', _external=True)

    sp_config = ShurjoPayConfigModel(
        username=saas_settings.get('gateway_store_id'),
        password=saas_settings.get('gateway_store_password'),
        base_url=api_endpoint,  # <--- RENAMED from 'endpoint'
        prefix=saas_settings.get('gateway_prefix'),
        return_url=return_url,
        cancel_url=cancel_url
    )
    
    shurjopay = ShurjopayPlugin(sp_config)
    
    shurjopay.logger.handlers = []
    shurjopay.logger.addHandler(logging.NullHandler())
    shurjopay.logger.propagate = False
    
    return shurjopay

def safe_verify_payment(saas_settings, order_id_from_sp):
    """
    Safe verification that uses 'base_url' instead of 'endpoint'.
    """
    print(f"Safely verifying order: {order_id_from_sp}")
    
    sp_config = get_shurjopay_config(saas_settings) 
    
    # FIX: Use 'base_url' attribute
    token_url = f"{sp_config.base_url}/api/get_token"
    token_payload = {
        "username": sp_config.username,
        "password": sp_config.password
    }
    
    token_res = requests.post(token_url, json=token_payload)
    token_res.raise_for_status() 
    token_data = token_res.json()
    
    if not token_data or 'token' not in token_data:
        raise Exception("ShurjoPay: Failed to get auth token for verification.")
    
    token = token_data['token']
    token_type = token_data['token_type']
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"{token_type} {token}"
    }
    
    # FIX: Use 'base_url' attribute
    verify_url = f"{sp_config.base_url}/api/verification"
    verify_payload = {"order_id": order_id_from_sp}
    
    response = requests.post(verify_url, headers=headers, json=verify_payload)
    response.raise_for_status() 
    
    response_data = response.json()
    
    if not response_data or not isinstance(response_data, list):
        raise Exception("ShurjoPay: Verification response was empty or not a list.")
        
    payment_details = response_data[0]
    
    cleaned_details = {}
    for key, value in payment_details.items():
        if value is None:
            if key in ['amount', 'discount_amount', 'usd_amt']:
                cleaned_details[key] = 0.0
            else:
                cleaned_details[key] = ""
        else:
            cleaned_details[key] = value
            
    return cleaned_details
# --- *** END OF NEW FUNCTION *** ---

def clean_shurjopay_response(response_dict):
    """
    The ShurjoPay plugin's verify_payment function returns None for
    some number fields, which crashes float(). This function cleans the dict.
    """
    if not isinstance(response_dict, dict):
        return response_dict # Not a dict, just return it
    
    cleaned_details = {}
    for key, value in response_dict.items():
        if value is None:
            # If value is None, set it to 0 for number fields
            # or an empty string for text fields
            if key in ['amount', 'discount_amount', 'usd_amt']:
                cleaned_details[key] = 0.0
            else:
                cleaned_details[key] = ""
        else:
            cleaned_details[key] = value
    return cleaned_details
# --- *** END OF NEW FUNCTION *** ---

# --- Corrected Audit Log Function ---
def log_portal_action(action_type: str, details: str):
    """Logs an action from the web portal to the audit log."""
    try:
        user = get_user_from_session()
        if not user:
            print("Audit Log (Portal): No user in session. Log skipped.")
            return

        employee_id = None
        employee_name = f"Customer: {user.get('customer_name', 'N/A')}"
        
        if user.get('is_employee'):
            employee_id = user['employee_id']
            employee_name = user['employee_name']

        payload = {
            "company_id": user['company_id'],
            "employee_id": employee_id,
            "employee_name": employee_name,
            "action_type": action_type,
            "details": details
        }
        
        supabase.table('audit_log').insert(payload).execute()
        print(f"Audit Logged (Portal): {action_type}")
        
    except Exception as e:
        print(f"CRITICAL PORTAL AUDIT LOG FAILURE: {e}")
        try:
            supabase.rpc('log_customer_action', {
                'p_company_id': user['company_id'],
                'p_customer_name': user.get('customer_name', 'N/A'),
                'p_action_type': action_type,
                'p_details': details
            }).execute()
            print(f"Audit Logged (Portal via RPC): {action_type}")
        except Exception as rpc_e:
            print(f"FINAL AUDIT LOG FAILURE: {rpc_e}")

# --- Decorators ---
def send_admin_notification(company_id, title, message, notif_type="General", related_id=None):
    """
    Sends a real-time notification to all admins.
    """
    try:
        admins_res = supabase.table('employees')\
            .select('id, employee_roles!inner(role_name)')\
            .eq('company_id', company_id)\
            .eq('employee_roles.role_name', 'Admin')\
            .execute()
            
        if not admins_res.data:
            return

        notifications = []
        # FIX: Generate TRUE UTC time (e.g., 2:00 PM instead of 8:00 PM)
        now_utc = datetime.now(timezone.utc).isoformat()
        
        for admin in admins_res.data:
            notifications.append({
                "company_id": company_id,
                "employee_id": admin['id'],
                "title": title,
                "message": message,
                "notification_type": notif_type,
                "related_id": related_id,
                "is_read": False,
                "created_at": now_utc # Send the corrected time
            })

        if notifications:
            supabase.table('app_notifications').insert(notifications).execute()
            print(f"--> Notification sent to {len(notifications)} admins.")

    except Exception as e:
        print(f"Failed to send admin notification: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not get_user_from_session():
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def employee_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user_from_session()
        if not user:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        if not user.get('is_employee'):
            flash('You do not have permission to access this page.', 'error')
            return redirect(url_for('dashboard_overview'))
        return f(*args, **kwargs)
    return decorated_function

def permission_required(permission_key):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_user_from_session()
            if not user:
                flash('Please log in to access this page.', 'error')
                return redirect(url_for('login'))
            if not user.get('is_employee'):
                 flash('You do not have permission to access this page.', 'error')
                 return redirect(url_for('dashboard_overview'))
            
            # This is where the 'str' object has no attribute 'get' error happened
            permissions = user.get('permissions', {})
            
            if user.get('role') != 'Admin' and not permissions.get(permission_key):
                flash('You do not have permission to access this page.', 'error')
                return redirect(url_for('employee_dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def customer_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user_from_session()
        if not user:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        if user.get('is_employee'):
            flash('This page is for customers only.', 'error')
            return redirect(url_for('employee_dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def get_portal_ads():
    """Fetches active portal ads and ensures they have IDs."""
    try:
        settings = get_saas_settings()
        ads = settings.get('portal_ads', []) or []
        
        active_ads = []
        for ad in ads:
            if ad.get('is_active'):
                # --- FIX: Handle legacy ads with missing IDs ---
                if not ad.get('id'):
                    # Generate a stable ID based on the image URL
                    # This ensures the same ad always gets the same ID for tracking
                    unique_str = f"{ad.get('image_url')}-{ad.get('redirect_url')}"
                    ad['id'] = hashlib.md5(unique_str.encode()).hexdigest()
                
                active_ads.append(ad)
                
        return sorted(active_ads, key=lambda x: int(x.get('display_order', 99)))
    except Exception as e:
        print(f"Error fetching ads: {e}")
        return []

# ==========================================
#  bKash Gateway Logic
# ==========================================
class BkashGateway:
    def __init__(self, username, password, app_key, app_secret, is_sandbox=True):
        self.username = username
        self.password = password
        self.app_key = app_key
        self.app_secret = app_secret
        # Determine Base URL
        self.base_url = "https://tokenized.sandbox.bka.sh/v1.2.0-beta" if is_sandbox else "https://tokenized.pay.bka.sh/v1.2.0-beta"
        self.headers = {'Content-Type': 'application/json', 'username': self.username, 'password': self.password}

    def get_token(self):
        url = f"{self.base_url}/tokenized/checkout/token/grant"
        payload = {"app_key": self.app_key, "app_secret": self.app_secret}
        
        res = requests.post(url, json=payload, headers=self.headers)
        data = res.json()
        
        if data.get('statusCode') == '0000':
            return data['id_token']
        raise Exception(f"bKash Token Error: {data.get('statusMessage')}")

    def create_payment(self, token, amount, invoice_number, callback_url):
        # --- SIMULATION MODE ---
        if self.username == 'demo':
            payment_id = f"MockPay_{invoice_number}_{datetime.now().strftime('%M%S')}"
            mock_url = url_for('mock_bkash_page', paymentID=payment_id, amount=amount, _external=True)
            return {
                'statusCode': '0000',
                'paymentID': payment_id,
                'bkashURL': mock_url
            }
        # -----------------------

        url = f"{self.base_url}/tokenized/checkout/create"
        headers = {
            'Authorization': token, 
            'X-APP-Key': self.app_key, 
            'Content-Type': 'application/json'
        }
        
        # --- THE FIX: Make Invoice Number Unique for Every Attempt ---
        # We append the current seconds to the invoice number so bKash sees it as new.
        import time
        unique_invoice_id = f"{invoice_number}_{int(time.time())}"
        
        payload = {
            "mode": "0011", 
            "payerReference": invoice_number, # This stays the real ID for reference
            "callbackURL": callback_url,
            "amount": str(amount), 
            "currency": "BDT", 
            "intent": "sale", 
            "merchantInvoiceNumber": unique_invoice_id # This must be unique every time
        }
        
        res = requests.post(url, json=payload, headers=headers)
        return res.json()

    def execute_payment(self, token, payment_id):
        url = f"{self.base_url}/tokenized/checkout/execute"
        headers = {'Authorization': token, 'X-APP-Key': self.app_key, 'Content-Type': 'application/json'}
        
        payload = {"paymentID": payment_id}
        
        res = requests.post(url, json=payload, headers=headers)
        return res.json()


@app.route('/')
def index():
    # 1. Check if user is logged in
    if 'user' in session:
        user = session['user']
        
        # 2. If Employee -> Send to Employee Dashboard
        if user.get('is_employee'):
            return redirect(url_for('employee_dashboard'))
            
        # 3. If Customer -> Send to Customer Dashboard
        return redirect(url_for('dashboard_overview'))
    
    # 4. If not logged in -> Send to Login Page
    return redirect(url_for('login'))

# --- *** ADDED MISSING PUBLIC ROUTES *** ---
@app.route('/purchase')
def purchase_plans():
    """
    Public page to display subscription plans and payment info for NEW clients.
    """
    saas_settings = get_saas_settings()
    
    # 1. Fetch SaaS Plans
    try:
        plans_res = supabase.table('saas_plans').select('*').order('price').execute()
        plans = plans_res.data or []
    except Exception as e:
        print(f"[DB_ERROR] /purchase (plans): {e}")
        plans = []
        
    # 2. Get Payment Details
    payment_details = {
        "bkash": saas_settings.get('payment_bkash'),
        "nagad": saas_settings.get('payment_nagad'),
        "bank": saas_settings.get('payment_bank_details')
    }

    # 3. Get Notice (This is no longer used, but harmless to fetch)
    saas_notice = saas_settings.get('saas_notice')

    return render_template(
        'purchase_plans.html',
        logo_url=saas_settings.get('saas_logo_url'),
        app_name=saas_settings.get('app_name', 'ISP Manager'),
        plans=plans,
        payment_details=payment_details,
        saas_notice=saas_notice,
        # We no longer pass products
    )

@app.route('/product/<uuid:product_id>')
def product_detail(product_id):
    """
    Shows the detailed page for a single e-commerce product
    AND fetches its approved reviews.
    """
    saas_settings = get_saas_settings()
    product = None
    reviews = []
    
    try:
        # 1. --- MODIFIED: Fetch the product WITH review data ---
        product_res = supabase.rpc('get_products_with_reviews')\
            .eq('id', str(product_id))\
            .maybe_single().execute()
        product = product_res.data
        # --- END MODIFICATION ---
        
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for('purchase_plans'))
            
        # 2. Fetch approved reviews for this product (This logic is unchanged)
        reviews_res = supabase.table('product_reviews')\
            .select('*')\
            .eq('product_id', str(product_id))\
            .eq('status', 'Approved')\
            .order('created_at', desc=True)\
            .execute()
        
        if reviews_res.data:
            reviews = reviews_res.data

    except Exception as e:
        print(f"[DB_ERROR] /product_detail: {e}")
        flash(f"Error fetching product: {e}", "error")
        return redirect(url_for('purchase_plans'))

    # Get today's date to check for discounts
    today_date = date.today().isoformat()

    return render_template(
        'product_detail.html',
        logo_url=saas_settings.get('saas_logo_url'),
        app_name=saas_settings.get('app_name', 'ISP Manager'),
        product=product,
        reviews=reviews, # Pass reviews to the page
        today_date=today_date,
        contact_email=saas_settings.get('contact_email', 'support@huda-it.com')
    )

@app.route('/contact')
def contact_us():
    """
    Public page to display SaaS Admin contact info.
    """
    saas_settings = get_saas_settings()
    
    contact_details = {
        "email": saas_settings.get('contact_email'),
        "phone": saas_settings.get('contact_phone'),
        "address": saas_settings.get('contact_address'),
        "facebook": saas_settings.get('social_facebook'),
        "youtube": saas_settings.get('social_youtube'),
        "linkedin": saas_settings.get('social_linkedin')
    }
    
    return render_template(
        'contact_us.html',
        logo_url=saas_settings.get('saas_logo_url'),
        app_name=saas_settings.get('app_name', 'ISP Manager'),
        contact=contact_details
    )


#
# --- *** MODIFIED CHECKOUT ROUTE (WITH EMAIL VALIDATION) *** ---
#
@app.route('/purchase/checkout/<uuid:plan_id>', methods=['GET', 'POST'])
def checkout(plan_id):
    saas_settings = get_saas_settings()
    
    try:
        plan_res = supabase.table('saas_plans').select('*').eq('id', plan_id).maybe_single().execute()
        plan = plan_res.data
        if not plan:
            flash("The plan you selected could not be found.", "error")
            return redirect(url_for('purchase_plans'))
    except Exception as e:
        flash(f"Error fetching plan: {e}", "error")
        return redirect(url_for('purchase_plans'))

    if request.method == 'POST':
        # --- NEW: Get the payment choice ---
        payment_choice = request.form.get('payment_method') # Will be 'pay_now' or 'pay_later'
        
        form_data = {
            "company_name": request.form.get('company_name'),
            "full_name": request.form.get('full_name'),
            "email": request.form.get('email').strip().lower(),
            "phone": request.form.get('phone'),
            "address": request.form.get('address')
        }

        if not ('@' in form_data['email'] and '.' in form_data['email']):
            flash("Please enter a valid email address.", "error")
            return redirect(url_for('checkout', plan_id=plan_id))

        try:
            # Check for existing active orders
            existing_order_res = supabase.table('saas_orders').select('status')\
                .eq('customer_details->>email', form_data['email'])\
                .in_('status', ['Pending Payment', 'Pending Review', 'Approved'])\
                .limit(1).execute()
            
            if existing_order_res.data:
                existing_status = existing_order_res.data[0]['status']
                flash(f"An active order with this email already exists (Status: {existing_status}). Please track your existing order or wait for approval.", "error")
                return redirect(url_for('checkout', plan_id=plan_id))
        except Exception as e:
            print(f"CRITICAL: Failed to check for existing orders: {e}")
            flash(f"An error occurred while checking your order: {e}", "error")
            return redirect(url_for('checkout', plan_id=plan_id))
        
        try:
            # Generate Order Number
            order_num_res = supabase.rpc('generate_new_order_number').execute()
            if not order_num_res.data:
                raise Exception("Failed to generate a new order number.")
            new_order_number = order_num_res.data
            
            # Calculate Price
            original_price = float(plan.get('price', 0))
            discount_percent = float(plan.get('discount_percent', 0))
            
            if discount_percent > 0:
                amount_to_pay = original_price * (1 - (discount_percent / 100))
            else:
                amount_to_pay = original_price
            amount_to_pay = max(1.0, float(f"{amount_to_pay:.2f}")) 

            plan_snapshot = {
                "name": plan.get('plan_name'),
                "price": original_price,
                "discount_percent": discount_percent,
                "final_price": amount_to_pay,
                "features": plan.get('features')
            }

            # Create the base order payload
            order_payload = {
                "order_number": new_order_number,
                "plan_id": plan.get('id'),
                "customer_details": form_data, 
                "plan_snapshot": plan_snapshot,
                "status": "Pending Payment" # Default status
            }
            
            # --- *** NEW LOGIC: Pay Now vs. Pay Later *** ---
            
            if payment_choice == 'pay_now':
                # --- SCENARIO 1: PAY NOW (Gateway ON) ---
                if not saas_settings.get('gateway_enabled', False):
                    flash("Online payments are not enabled. Please choose 'Pay Later' or contact support.", "error")
                    return redirect(url_for('checkout', plan_id=plan_id))
                
                shurjopay = initialize_shurjopay(saas_settings)
                payment_payload_dict = {
                    "amount": amount_to_pay, "order_id": new_order_number, 
                    "customer_name": form_data['full_name'], "customer_phone": form_data['phone'],
                    "customer_email": form_data['email'], "customer_city": "Dhaka", 
                    "customer_address": form_data['address'], "currency": "BDT", "customer_post_code": "1200"
                }
                payment_payload_obj = SimpleNamespace(**payment_payload_dict)
                response = shurjopay.make_payment(payment_payload_obj)
                
                if isinstance(response, dict):
                    raise Exception(f"ShurjoPay Error: {response.get('message', 'Unknown error.')}")
                
                if hasattr(response, 'checkout_url') and response.checkout_url:
                    order_payload['checkout_url'] = response.checkout_url
                    order_payload['gateway_tx_id'] = response.sp_order_id # Use new column
                    
                    order_res = supabase.table('saas_orders').insert(order_payload).execute()
                    if not order_res.data:
                        raise Exception("Failed to save pending order to database.")
                    
                    # --- *** FIX: SEND ADMIN & CUSTOMER EMAILS *** ---
                    # (Send Customer Email)
                    try:
                        # Send the tracking link, which will show the "Pay Now" button
                        track_url = url_for('order_status', order_number=new_order_number, _external=True)
                        email_service.send_order_confirmation_email(
                            saas_settings, form_data['email'], form_data['company_name'],
                            new_order_number, plan_snapshot, track_url=track_url
                        )
                    except Exception as e:
                        print(f"Warning: Failed to send CUSTOMER order confirmation email: {e}")
                    
                    # (Send Admin Email)
                    try:
                        admin_email = saas_settings.get('contact_email')
                        if admin_email:
                            admin_html_body = render_template('order_notification_email.html', 
                                                              form_data=form_data, 
                                                              plan=plan_snapshot,
                                                              payment_details=None) # No payment details yet
                            email_service.send_generic_email(
                                saas_settings, admin_email,
                                f"New Plan Order (Pending Payment): {plan_snapshot['name']} for {form_data['company_name']}",
                                admin_html_body
                            )
                            print(f"Admin notification for 'Pay Now' order sent to {admin_email}")
                    except Exception as e:
                        print(f"Warning: Failed to send ADMIN order notification email: {e}")
                    # --- *** END OF FIX *** ---

                    return redirect(response.checkout_url) # Redirect to gateway
                else:
                    raise Exception("ShurjoPay failed to return a checkout_url.")

            elif payment_choice == 'pay_later':
                # --- SCENARIO 2: PAY LATER (Gateway OFF logic) ---
                
                order_res = supabase.table('saas_orders').insert(order_payload).execute()
                if not order_res.data:
                    raise Exception("Failed to save order to database.")
                
                try:
                    pay_now_url = url_for('pay_for_order', order_number=new_order_number, _external=True)
                    email_service.send_order_confirmation_email(
                        saas_settings, 
                        to_email=form_data['email'], 
                        company_name=form_data['company_name'],
                        order_number=new_order_number, 
                        plan_snapshot=plan_snapshot, 
                        pay_now_url=pay_now_url
                    )
                except Exception as e:
                    print(f"Warning: Failed to send CUSTOMER order confirmation email: {e}")
                
                try:
                    admin_email = saas_settings.get('contact_email')
                    if admin_email:
                        admin_html_body = render_template('order_notification_email.html', 
                                                          form_data=form_data, 
                                                          plan=plan_snapshot,
                                                          payment_details=None) # No payment details
                        email_service.send_generic_email(
                            saas_settings, admin_email,
                            f"New Plan Order: {plan_snapshot['name']} for {form_data['company_name']}",
                            admin_html_body
                        )
                except Exception as e:
                    print(f"Warning: Failed to send ADMIN order notification email: {e}")
                
                flash("Your order has been placed! A confirmation email has been sent.", "success")
                return redirect(url_for('order_status', order_number=new_order_number))

            else:
                flash("Invalid payment choice.", "error")
                return redirect(url_for('checkout', plan_id=plan_id))
                
        except Exception as e:
            print(f"CRITICAL: Failed to process order: {e}")
            flash(f"An error occurred: {e}", "error")
            return redirect(url_for('checkout', plan_id=plan_id))

    # --- GET Request Logic ---
    return render_template('checkout.html', 
                           plan=plan,
                           developer_logo=saas_settings.get('saas_logo_url'),
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           contact_email=saas_settings.get('contact_email', 'support@huda-it.com'))
# --- END OF CHECKOUT ROUTE ---

@app.context_processor
def inject_cart_count():
    """Makes 'cart_item_count' available to all templates."""
    cart = session.get('cart', {})
    cart_item_count = sum(cart.values())
    return dict(cart_item_count=cart_item_count)

# --- ADD THIS HELPER FUNCTION BEFORE THE 'cart' ROUTE ---
def calculate_final_price(product):
    """
    Robustly calculates price & discount. 
    Handles None/Null values safely to prevent errors.
    """
    try:
        # 1. Get Price safely
        original_price = float(product.get('selling_price') or 0)
        
        # 2. Get Discount safely (Handle None)
        raw_percent = product.get('discount_percent')
        if raw_percent is None:
            percent = 0.0
        else:
            percent = float(raw_percent)
            
        # 3. Get Dates safely
        today = date.today().isoformat()
        start = str(product.get('discount_start_date') or '1970-01-01')
        end = str(product.get('discount_end_date') or '2099-12-31')
        
        # 4. Check Validity
        if percent > 0 and start <= today and end >= today:
            discount_amount = original_price * (percent / 100)
            final_price = original_price - discount_amount
            return final_price, True, percent # Returns: (Price, Is_Discounted?, Percent)
            
    except Exception as e:
        print(f"Price Calc Error for product {product.get('id')}: {e}")
        
    # Default return if no discount or error
    return float(product.get('selling_price') or 0), False, 0.0


# --- REPLACE YOUR EXISTING 'cart' ROUTE ---
@app.route('/cart')
def cart():
    """Displays cart items using robust calculation."""
    saas_settings = get_saas_settings()
    cart = session.get('cart', {})
    
    shipping_cost = float(saas_settings.get('shipping_cost', 0.0))
    cart_products = []
    subtotal = 0
    
    if cart:
        product_ids = list(cart.keys())
        fetched_products = []
        
        try:
            # Try RPC first
            res = supabase.rpc('get_products_with_reviews', {'p_search_term': ""}).execute()
            if res.data:
                # Filter in Python to ensure we match IDs correctly
                fetched_products = [p for p in res.data if str(p['id']) in product_ids]
            
            # Fallback to direct table query
            if not fetched_products:
                res_table = supabase.table('products').select('*, category_id').in_('id', product_ids).execute()
                fetched_products = res_table.data or []
                    
        except Exception as e:
            print(f"Cart Fetch Error: {e}")
        
        for product in fetched_products:
            product_id = str(product['id'])
            if product_id not in cart: continue
                
            quantity = int(cart[product_id])
            
            # --- USE THE ROBUST HELPER ---
            final_price, is_discounted, percent = calculate_final_price(product)
            original_price = float(product.get('selling_price', 0))
            
            item_subtotal = final_price * quantity
            subtotal += item_subtotal
            
            cart_products.append({
                "id": product_id,
                "name": product.get('name'),
                "image_url": product.get('image_url'),
                "quantity": quantity,
                "original_price": original_price,
                "final_price_per_item": final_price,
                "is_discounted": is_discounted,
                "discount_percent": percent, # Pass this to template
                "subtotal": item_subtotal,
                "category_id": product.get('category_id')
            })
    
    # Promo Logic
    promo = session.get('promo')
    discount_amount = 0.0
    if promo:
        if promo['type'] == 'Percentage':
            discount_amount = subtotal * (promo['value'] / 100)
        else:
            discount_amount = promo['value']
        if discount_amount > subtotal: discount_amount = subtotal
            
    total_price = max(0, subtotal + shipping_cost - discount_amount)

    return render_template(
        'product_cart.html',
        logo_url=saas_settings.get('saas_logo_url'),
        app_name=saas_settings.get('app_name', 'ISP Manager'),
        contact_email=saas_settings.get('contact_email', 'support@huda-it.com'),
        cart_products=cart_products,
        subtotal=subtotal,
        shipping_cost=shipping_cost,
        promo=promo,
        discount_amount=discount_amount,
        total_price=total_price
    )
# --- *** NEW: Promo Code Routes *** ---

@app.route('/apply-promo', methods=['POST'])
def apply_promo():
    """Validates and applies a promo code to the session with detailed error messages."""
    code = request.form.get('promo_code', '').strip().upper()
    
    if not code:
        flash("Please enter a promo code to apply a discount.", "error")
        return redirect(url_for('cart'))
        
    try:
        # --- *** THIS IS THE FIX *** ---
        # We use standard .execute() instead of .maybe_single() to avoid the crash
        res = supabase.table('product_promos').select('*').eq('code', code).execute()
        
        # Check if we got any results
        if not res.data or len(res.data) == 0:
            flash(f"The promo code '{code}' is invalid. Please check the spelling.", "error")
            return redirect(url_for('cart'))
            
        promo = res.data[0] # Get the first result
        # --- *** END OF FIX *** ---
        
        # --- Scenario 2: Code is manually set to Inactive ---
        if promo['status'] != 'Active':
            flash(f"The promo code '{code}' is currently inactive.", "error")
            return redirect(url_for('cart'))
            
        today = date.today().isoformat()
        
        # --- Scenario 3: Date validation ---
        if today < promo['start_date']:
            flash(f"The promo code '{code}' is not active yet. It starts on {promo['start_date']}.", "error")
            return redirect(url_for('cart'))
            
        if today > promo['end_date']:
            flash(f"The promo code '{code}' has expired.", "error")
            return redirect(url_for('cart'))
            
        # --- Scenario 4: Usage Limit Reached ---
        if promo['usage_count'] >= promo['usage_limit']:
            flash(f"The promo code '{code}' has reached its maximum usage limit and is no longer available.", "error")
            return redirect(url_for('cart'))
            
        # --- Scenario 5: Item Targeting (Product/Category mismatch) ---
        cart = session.get('cart', {})
        if not cart:
            flash("Your cart is empty. Please add items before applying a promo code.", "error")
            return redirect(url_for('cart'))
            
        target_pid = promo.get('target_product_id')
        target_cid = promo.get('target_category_id')
        
        # If the promo targets specific items, we must check if they are in the cart
        if target_pid or target_cid:
            cart_product_ids = list(cart.keys())
            is_eligible = False
            
            # Check Product Match
            if target_pid:
                if str(target_pid) in cart_product_ids:
                    is_eligible = True
            
            # Check Category Match
            elif target_cid:
                prods_res = supabase.table('products').select('category_id').in_('id', cart_product_ids).execute()
                if prods_res.data:
                    for p in prods_res.data:
                        if str(p.get('category_id')) == str(target_cid):
                            is_eligible = True
                            break
            
            if not is_eligible:
                flash(f"The promo code '{code}' is valid, but it does not apply to the items currently in your cart.", "error")
                return redirect(url_for('cart'))

        # --- Success! Store in session ---
        session['promo'] = {
            'code': promo['code'],
            'type': promo['discount_type'],
            'value': float(promo['discount_value']),
            'id': promo['id'],
            'target_product_id': promo.get('target_product_id'),
            'target_category_id': promo.get('target_category_id')
        }
        flash(f"Promo code '{code}' applied successfully!", "success")
        
    except Exception as e:
        print(f"Error applying promo: {e}")
        # Generic error for unexpected system issues
        flash("An unexpected error occurred while checking the promo code.", "error")
        
    return redirect(url_for('cart'))

@app.route('/remove-promo')
def remove_promo():
    """Removes the applied promo code from the session."""
    session.pop('promo', None)
    flash("Promo code removed.", "info")
    return redirect(url_for('cart'))

# --- *** END NEW ROUTES *** ---

@app.route('/add-to-cart/<uuid:product_id>', methods=['POST'])
def add_to_cart(product_id):
    """Adds item to cart with Python-side filtering to bypass DB glitches."""
    product_id_str = str(product_id)
    target_product = None
    
    try:
        # Strategy: Fetch list and find item in Python (Bypasses RLS/Filter issues)
        # 1. Try RPC
        res = supabase.rpc('get_products_with_reviews', {'p_search_term': ""}).execute()
        if res.data:
            # Find product in the list
            target_product = next((p for p in res.data if str(p['id']) == product_id_str), None)
            
        # 2. Fallback to Table if RPC missed it (e.g. for Guests)
        if not target_product:
            res_table = supabase.table('products').select('*').eq('id', product_id_str).execute()
            if res_table.data:
                target_product = res_table.data[0]

        # 3. Check if found and stock
        if not target_product:
            flash("Product not found.", "error")
            return redirect(request.referrer or url_for('shop'))
            
        if target_product.get('stock_quantity', 0) <= 0:
            flash(f"Sorry, '{target_product.get('name')}' is out of stock.", "error")
            return redirect(request.referrer or url_for('shop'))
            
    except Exception as e:
        print(f"Stock Check Error: {e}")
        # Fail safe: Allow adding if we can't check (prevent blocking sales)
        # OR return error. Let's return error to be safe.
        flash("Could not verify stock. Please try again.", "error")
        return redirect(request.referrer or url_for('shop'))

    # Success
    cart = session.get('cart', {})
    cart[product_id_str] = cart.get(product_id_str, 0) + 1
    session['cart'] = cart
    
    p_name = target_product.get('name', 'Item')
    flash(f"'{p_name}' added to cart!", "success")
    return redirect(request.referrer or url_for('shop'))


# --- 2. ROBUST BUY NOW ---
@app.route('/buy-now/<uuid:product_id>', methods=['POST'])
def buy_now(product_id):
    """Buy Now with Python-side filtering."""
    product_id_str = str(product_id)
    target_product = None
    
    try:
        # 1. Fetch via RPC
        res = supabase.rpc('get_products_with_reviews', {'p_search_term': ""}).execute()
        if res.data:
            target_product = next((p for p in res.data if str(p['id']) == product_id_str), None)
        
        # 2. Fallback
        if not target_product:
            res_table = supabase.table('products').select('stock_quantity').eq('id', product_id_str).execute()
            if res_table.data:
                target_product = res_table.data[0]

        if not target_product:
            flash("Product not found.", "error")
            return redirect(request.referrer or url_for('shop'))
            
        if target_product.get('stock_quantity', 0) <= 0:
            flash("Sorry, this item is out of stock.", "error")
            return redirect(request.referrer or url_for('shop'))
            
    except Exception as e:
        print(f"Buy Now Error: {e}")
        flash("Error processing request.", "error")
        return redirect(request.referrer or url_for('shop'))

    cart = session.get('cart', {})
    cart[product_id_str] = cart.get(product_id_str, 0) + 1
    session['cart'] = cart
    
    return redirect(url_for('cart'))
@app.route('/update-cart/<uuid:product_id>', methods=['POST'])
def update_cart(product_id):
    """Updates the quantity of an item in the cart."""
    product_id_str = str(product_id)
    try:
        quantity = int(request.form.get('quantity', 1))
    except ValueError:
        quantity = 1
        
    cart = session.get('cart', {})
    
    if quantity > 0:
        cart[product_id_str] = quantity
    elif product_id_str in cart:
        # If quantity is 0 or less, remove it
        cart.pop(product_id_str, None)
        
    session['cart'] = cart
    return redirect(url_for('cart'))

@app.route('/remove-from-cart/<uuid:product_id>', methods=['POST'])
def remove_from_cart(product_id):
    """Removes an item from the cart completely."""
    product_id_str = str(product_id)
    cart = session.get('cart', {})
    
    if product_id_str in cart:
        cart.pop(product_id_str, None)
        flash("Item removed from cart.", "info")
        
    session['cart'] = cart
    return redirect(url_for('cart'))

# --- *** END OF NEW FUNCTIONS *** ---

# --- ADD THESE NEW ROUTES after your checkout function ---

@app.route('/payment/return', methods=['GET'])
def shurjopay_return():
    """
    Callback URL for ShurjoPay.
    """
    order_id_from_sp = request.args.get('order_id')

    if not order_id_from_sp or order_id_from_sp.startswith("NOK"):
        flash("Payment was cancelled or failed. You can try again from your order status page.", "error")
        return redirect(url_for('track_order')) # Redirect to search page

    saas_settings = get_saas_settings()
    is_sandbox = saas_settings.get('gateway_sandbox_enabled', True)
    
    try:
        response = None 
        
        if is_sandbox:
            print("SANDBOX MODE: Simulating successful payment verification.")
            response = {
                "sp_code": 1000, "message": "Sandboxed Payment Success",
                "order_id": order_id_from_sp, "method": "Sandbox Test Card",
                "bank_trx_id": "SP_SANDBOX_DUMMY_TXID"
            }
        else:
            print("LIVE MODE: Attempting to verify payment.")
            response = safe_verify_payment(saas_settings, order_id_from_sp)
            
        if isinstance(response, dict) and response.get('sp_code') == 1000:
            # --- PAYMENT IS VERIFIED OR SIMULATED AS SUCCESSFUL ---
            
            # 'order_id' from the response is our gateway_tx_id
            gateway_tx_id = response.get('order_id') 
            order_res = supabase.table('saas_orders')\
                .select('*')\
                .eq('gateway_tx_id', gateway_tx_id)\
                .maybe_single().execute()
            
            if not order_res.data:
                raise Exception(f"Payment verified, but no matching order found for gateway_tx_id: {gateway_tx_id}")
            
            order = order_res.data
            
            # --- MODIFIED: Update status and add transaction ID ---
            update_payload = {
                "status": "Pending Review", # NEW status
                "payment_method": response.get('method'),
                "transaction_id": response.get('bank_trx_id') # Use new column
            }
            
            supabase.table('saas_orders').update(update_payload).eq('id', order['id']).execute()
            
            form_data = order.get('customer_details', {})
            plan_snapshot = order.get('plan_snapshot', {})
            
            # (Send Customer "Cash Memo" Email)
            try:
                track_url = url_for('order_status', order_number=order['order_number'], _external=True)
                email_service.send_order_confirmation_email(
                    saas_settings, 
                    to_email=form_data.get('email'), 
                    company_name=form_data.get('company_name'),
                    order_number=order['order_number'], 
                    plan_snapshot=plan_snapshot,
                    track_url=track_url,
                    payment_details=response # Pass the payment details
                )
            except Exception as e:
                print(f"Warning: Failed to send CUSTOMER (paid) confirmation email: {e}")
            
            # (Send Admin "PAID Order" Email)
            try:
                admin_email = saas_settings.get('contact_email')
                if admin_email:
                    admin_html_body = render_template('order_notification_email.html', 
                                                      form_data=form_data, 
                                                      plan=plan_snapshot,
                                                      payment_details=response)
                    
                    email_service.send_generic_email(
                        saas_settings, admin_email,
                        f"New PAID Order: {plan_snapshot.get('name')} for {form_data['company_name']}",
                        admin_html_body
                    )
            except Exception as e:
                print(f"Warning: Failed to send ADMIN (paid) notification email: {e}")

            flash("Payment successful! Your order is now pending review.", "success")
            return redirect(url_for('order_status', order_number=order['order_number']))
            
        else:
            # --- PAYMENT FAILED OR WAS NOT VERIFIED ---
            error_msg = response.get('message', 'Payment verification failed.')
            flash(f"Payment Failed: {error_msg}. You can try again or select 'Pay Later' on your order status page.", "error")
            return redirect(url_for('track_order'))

    except Exception as e:
        print(f"CRITICAL: Failed to process payment return: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('track_order'))


@app.route('/payment/cancel', methods=['GET', 'POST'])
def shurjopay_cancel():
    """
    Callback URL if the user cancels the payment.
    """
    flash("Your payment was cancelled. You can try again at any time.", "info")
    return redirect(url_for('purchase_plans'))

# --- END OF NEW ROUTES ---

#
# --- *** ADD THIS NEW ROUTE TO APP.PY *** ---
#
@app.route('/pay-for-order/<string:order_number>', methods=['GET'])
def pay_for_order(order_number):
    """
    Finds an existing 'Pending Payment' order and redirects
    the user to the payment gateway to pay for it.
    """
    saas_settings = get_saas_settings()
    
    if not saas_settings.get('gateway_enabled', False):
        flash("Online payments are not enabled. Please contact support.", "error")
        return redirect(url_for('order_status', order_number=order_number))
        
    try:
        # Find the order
        order_res = supabase.table('saas_orders')\
            .select('*')\
            .eq('order_number', order_number)\
            .eq('status', 'Pending Payment')\
            .is_('payment_method', None) \
            .maybe_single().execute()
            
        if not order_res.data:
            flash("This order is not eligible for payment. It may already be paid or has been rejected.", "error")
            return redirect(url_for('order_status', order_number=order_number))
            
        order = order_res.data
        form_data = order.get('customer_details', {})
        plan_snapshot = order.get('plan_snapshot', {})
        amount_to_pay = plan_snapshot.get('final_price', 1.0)
        
        shurjopay = initialize_shurjopay(saas_settings)
        
        payment_payload_dict = {
            "amount": amount_to_pay,
            "order_id": order.get('order_number'), 
            "customer_name": form_data.get('full_name'),
            "customer_phone": form_data.get('phone'),
            "customer_email": form_data.get('email'),
            "customer_city": "Dhaka", 
            "customer_address": form_data.get('address'),
            "currency": "BDT",
            "customer_post_code": "1200"
        }
        payment_payload_obj = SimpleNamespace(**payment_payload_dict)
        
        response = shurjopay.make_payment(payment_payload_obj)
        
        if isinstance(response, dict):
            raise Exception(f"ShurjoPay Error: {response.get('message', 'Unknown error.')}")
        
        if hasattr(response, 'checkout_url') and response.checkout_url:
            # Save the new gateway details, overwriting any old ones
            update_payload = {
                "checkout_url": response.checkout_url,
                "gateway_tx_id": response.sp_order_id # Use new column
            }
            supabase.table('saas_orders').update(update_payload).eq('id', order['id']).execute()
            
            return redirect(response.checkout_url)
        else:
            raise Exception("ShurjoPay failed to return a checkout_url.")

    except Exception as e:
        print(f"CRITICAL: Failed to create 'Pay Now' link: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('order_status', order_number=order_number))



# --- *** MODIFIED LOGIN ROUTE (uses get_saas_settings) *** ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_user_from_session(): return redirect(url_for('index'))
    
    last_email = request.cookies.get(REMEMBER_EMAIL_COOKIE)

    # --- MODIFIED: Use get_saas_settings() for logo ---
    saas_settings = get_saas_settings()
    logo_url = saas_settings.get('saas_logo_url')
    app_name = saas_settings.get('app_name', 'ISP Manager')
    contact_email = saas_settings.get('contact_email', 'support@huda-it.com')
    developer_logo = logo_url  
    saas_notice = saas_settings.get('saas_notice')
    featured_clients = saas_settings.get('featured_clients', [])
    # --- END MODIFICATION ---
    
    if request.method == 'POST':
        username = request.form.get('username'); password = request.form.get('password')
        remember_me = request.form.get('remember')
        
        if not username or not password:
            flash('Email/Phone and password required.', 'error'); return redirect(url_for('login'))
        if not supabase:
             flash('Database connection error.', 'error'); return redirect(url_for('login'))

        auth_response = None
        try:
            # --- Step 1: Authenticate with Supabase Auth ---
            login_method = 'email' if '@' in username else 'phone'
            username_normalized = username.lower() if login_method == 'email' else username

            if login_method == 'email':
                auth_response = supabase.auth.sign_in_with_password({"email": username_normalized, "password": password})
            else:
                 flash('Phone number login not yet supported. Please use email.', 'error'); return redirect(url_for('login'))

            if auth_response and auth_response.user:
                auth_id = auth_response.user.id
                auth_email = auth_response.user.email
                
                # --- *** THIS IS THE FIX *** ---
                # Add these lines to activate the user's RLS permissions
                # This MUST happen before any other database calls.
                try:
                    supabase.auth.set_session(
                        auth_response.session.access_token,  
                        auth_response.session.refresh_token
                    )
                except Exception as e:
                    print(f"CRITICAL: Failed to set session - {e}")
                    flash("Login failed: Could not activate session.", "error")
                    return redirect(url_for('login'))
                # --- *** END OF FIX *** ---

                # --- Step 2: Find the user in the 'customers' table ---
                # Now this query will work correctly for customers
                customer_response = None
                try:
                    customer_response = supabase.table('customers').select(
                        'id, full_name, company_id, status, package_id, profile_avatar_url, zone_id'
                    ).eq('user_id', auth_id).maybe_single().execute()
                except APIError as e:
                    if "Missing response" not in e.message:
                        raise # Re-raise other API errors
                
                if customer_response and customer_response.data:
                    customer = customer_response.data
                    if customer.get('status') != 'Active':
                         flash('Account inactive/suspended. Contact support.', 'error'); supabase.auth.sign_out(); return redirect(url_for('login'))
                    
                    company_id = customer['company_id']; company_info = {}
                    if company_id:
                        try:
                            # --- RLS FIX: Only select NON-SENSITIVE info ---
                            company_response = supabase.table('isp_companies').select(
                                'company_name, logo_url, social_media_links, company_details, payment_info, developer_logo_url'
                            ).eq('id', company_id).maybe_single().execute()
                            
                            # --- *** FIX for 'NoneType' object *** ---
                            if company_response and company_response.data: 
                                company_info = company_response.data
                        except APIError as e:
                             if "Missing response" in e.message:
                                 print(f"Login Warning: Customer {customer['id']} linked to missing company_id {company_id}")
                                 company_info = {} # Continue with default info
                             else:
                                 raise
                    
                    session['user'] = { 
                        'auth_id': auth_id, 'email': auth_email, 'customer_id': customer['id'], 
                        'customer_name': customer['full_name'], 'avatar_url': customer.get('profile_avatar_url'), 
                        'company_id': customer['company_id'], 'package_id': customer.get('package_id'),
                        'zone_id': customer.get('zone_id'), 'company_name': company_info.get('company_name', 'Your ISP'), 
                        'company_logo': company_info.get('logo_url'), 'social_media': company_info.get('social_media_links'), 
                        'company_details': company_info.get('company_details'), 'payment_info': company_info.get('payment_info'),
                        'developer_logo': company_info.get('developer_logo_url'), 'is_employee': False
                    }
                    
                    response = make_response(redirect(url_for('dashboard_overview')))
                    if remember_me: response.set_cookie(REMEMBER_EMAIL_COOKIE, username, max_age=60*60*24*30)
                    else: response.set_cookie(REMEMBER_EMAIL_COOKIE, '', expires=0)
                    return response

                # --- Step 3: If not a customer, check 'employees' table ---
                employee_response = None
                try:
                    employee_response = supabase.table('employees').select(
                        'id, full_name, company_id, status, role_id, profile_avatar_url, '
                        'employee_roles(role_name, permissions)'
                    ).eq('user_id', auth_id).maybe_single().execute()
                except APIError as e:
                    if "Missing response" not in e.message:
                        raise
                
                if employee_response and employee_response.data:
                    employee = employee_response.data
                    if employee.get('status') != 'Active':
                        flash('Employee account inactive/suspended. Contact admin.', 'error'); supabase.auth.sign_out(); return redirect(url_for('login'))

                    company_id = employee.get('company_id')
                    if not company_id:
                        raise Exception("Employee record is missing a company ID.")

                    company_info = {}
                    try:
                        company_response = supabase.table('isp_companies').select(
                            'company_name, logo_url, social_media_links, company_details, payment_info, developer_logo_url'
                        ).eq('id', company_id).maybe_single().execute()
                        
                        if company_response and company_response.data: 
                            company_info = company_response.data
                        else:
                            raise Exception("This company's account is not active or cannot be found.")
                    except APIError as e:
                        if "Missing response" in e.message:
                            print(f"Login Error: Employee {employee['id']} linked to missing company_id {company_id}")
                            raise Exception("Login failed: Your linked company does not exist.")
                        else:
                            raise
                    
# --- *** ROBUST FIX for 'str' object has no attribute 'get' *** ---
                    
                    # Use 'or {}' to ensure employee_role_data is a dict, not None
                    employee_role_data = employee.get('employee_roles') or {}
                    
                    # Get the permissions value. It could be a dict, a str, or None
                    permissions_data = employee_role_data.get('permissions') 

                    if isinstance(permissions_data, str):
                        try:
                            # This will parse "{\"key\": true}" (good)
                            permissions_data = json.loads(permissions_data)
                        except json.JSONDecodeError:
                            # This will fail on "{'key': true}" (bad)
                            print(f"Login Warning: Could not parse permissions string for employee {employee['id']}")
                            permissions_data = {} # Default on error
                    elif not isinstance(permissions_data, dict):
                        # This catches 'None' or any other bad data type
                        permissions_data = {}
                    # --- *** END OF FIX *** ---

                    session['user'] = { 
                        'auth_id': auth_id, 'email': auth_email, 'employee_id': employee['id'], 
                        'employee_name': employee['full_name'], 'avatar_url': employee.get('profile_avatar_url'), 
                        'role': employee_role_data.get('role_name', 'Employee'),
                        'permissions': permissions_data, # <-- Use the new, clean variable
                        'company_id': employee['company_id'], 'company_name': company_info.get('company_name', 'Your ISP'), 
                        'company_logo': company_info.get('logo_url'), 'social_media': company_info.get('social_media_links'), 
                        'company_details': company_info.get('company_details'), 'is_employee': True
                    }
                    
                    response = make_response(redirect(url_for('employee_dashboard')))
                    if remember_me: response.set_cookie(REMEMBER_EMAIL_COOKIE, username, max_age=60*60*24*30)
                    else: response.set_cookie(REMEMBER_EMAIL_COOKIE, '', expires=0)
                    return response

                # --- Step 4: If user exists in Auth but not in tables ---
                print(f"Auth successful for {auth_email} (ID: {auth_id}), but user not found in 'customers' or 'employees' table.")
                flash('Login OK, but user is not linked to this company. Contact admin.', 'error'); 
                supabase.auth.sign_out(); 
                return redirect(url_for('login'))
            
            # --- This 'else' handles failed login (e.g., wrong password) ---
            else:
                 error_msg = "Invalid credentials."
                 if hasattr(auth_response, 'error') and auth_response.error:
                      error_msg = auth_response.error.message
                 
                 if "Email not confirmed" in error_msg: 
                      flash("Confirm email first.", 'error')
                 else: 
                      flash(error_msg, 'error')
                 print(f"Login failed: {error_msg}"); 
                 return redirect(url_for('login'))

        # --- This 'except' handles *actual* errors (e.g., network, auth server down, or code bugs) ---
        except (AuthApiError, APIError, Exception) as e: 
            error_message = getattr(e, 'message', str(e))
            if "Missing response" in error_message:
                error_message = "Invalid credentials or user not found."
                
            print(f"Login Error: {error_message}"); 
            flash(f"Error: {error_message}", 'error'); 
            return redirect(url_for('login'))

    return render_template('login.html', last_email=last_email, developer_logo=developer_logo, contact_email=contact_email, saas_notice=saas_notice, featured_clients=featured_clients)

# --- *** MODIFIED SIGNUP ROUTE (uses get_saas_settings) *** ---
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if get_user_from_session(): return redirect(url_for('index'))
    
    saas_settings = get_saas_settings()
    logo_url = saas_settings.get('saas_logo_url')
    contact_email = saas_settings.get('contact_email', 'support@huda-it.com')
    developer_logo = logo_url

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        
        print(f"SIGNUP DEBUG: Email='{email}'") # Debug print

        # Validation
        if not email:
            flash('Email field is required.', 'error')
            return redirect(url_for('signup'))
        if not password:
            flash('Password field is required.', 'error')
            return redirect(url_for('signup'))
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('signup'))

        try:
            # --- 1. SECURITY CHECK (Robust) ---
            
            # Check Customers
            cust_res = supabase.table('customers').select('id, user_id').eq('email', email).maybe_single().execute()
            # Check Employees
            emp_res = supabase.table('employees').select('id, user_id').eq('email', email).maybe_single().execute()
            
            target_table = None
            target_id = None
            
            # Handle Customer Match
            # FIX: Check if 'cust_res' exists AND 'cust_res.data' exists
            if cust_res and cust_res.data:
                if cust_res.data.get('user_id'):
                    flash("Account already active. Please Log In.", "info")
                    return redirect(url_for('login'))
                target_table = 'customers'
                target_id = cust_res.data['id']
                
            # Handle Employee Match
            # FIX: Check if 'emp_res' exists AND 'emp_res.data' exists
            elif emp_res and emp_res.data:
                if emp_res.data.get('user_id'):
                    flash("Account already active. Please Log In.", "info")
                    return redirect(url_for('login'))
                target_table = 'employees'
                target_id = emp_res.data['id']
            
            else:
                # If neither returned data, the email is not in the system
                flash("Registration Restricted: This email is not found in our system. Please ask your Admin to add you first.", "error")
                return redirect(url_for('signup'))

            # --- 2. Create Auth User ---
            auth_response = supabase.auth.sign_up({
                "email": email, 
                "password": password
            })
            
            # FIX: Check if auth_response itself is valid
            if not auth_response or not auth_response.user:
                 # Sometimes it returns None on error instead of raising exception
                 print(f"Auth Failed: Response was {auth_response}")
                 flash("Signup failed. Please check your email and password.", "error")
                 return redirect(url_for('signup'))
            
            new_user_id = auth_response.user.id

            # --- 3. Link the Profile ---
            if target_table and target_id:
                supabase.table(target_table).update({'user_id': new_user_id}).eq('id', target_id).execute()
            
            flash("Account successfully created! You can now log in.", "success")
            return redirect(url_for('login'))

        except Exception as e:
            error_msg = str(e)
            print(f"Signup Exception: {error_msg}")
            
            if "already registered" in error_msg or "unique constraint" in error_msg:
                flash("Account already exists. Please log in.", "warning")
                return redirect(url_for('login'))
            
            flash(f"Signup failed: {error_msg}", "error")
            return redirect(url_for('signup'))

    return render_template('signup.html', developer_logo=developer_logo, contact_email=contact_email)

# --- *** ADDED MISSING RESET PASSWORD ROUTE *** ---
@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if get_user_from_session(): return redirect(url_for('index'))

    saas_settings = get_saas_settings()
    logo_url = saas_settings.get('saas_logo_url')
    app_name = saas_settings.get('app_name', 'ISP Manager')
    contact_email = saas_settings.get('contact_email', 'support@huda-it.com')
    developer_logo = logo_url

    if request.method == 'POST':
        email = request.form.get('email')
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for('reset_password'))
        try:
            supabase.auth.reset_password_email(email)
            flash("If an account exists for this email, a password reset link has been sent.", 'info')
            return redirect(url_for('login'))
        except Exception as e:
            error_message = getattr(e, 'message', str(e))
            print(f"Error sending reset password: {e}")
            flash(f"Error: {error_message}", 'error')
    
    return render_template('reset_password.html', developer_logo = logo_url, app_name=app_name, contact_email=contact_email)
# --- *** END OF ADDED ROUTE *** ---


# --- CUSTOMER ROUTES ---

@app.route('/dashboard-overview')
@customer_login_required
def dashboard_overview():
    user = get_user_from_session()
    customer_id = user['customer_id']
    customer_zone_id = user.get('zone_id')  
    
    open_ticket_count = 0
    unpaid_invoice_count = 0
    upcoming_appt_count = 0  
    network_updates = []
    has_error = False  

    # --- Existing Logic: Tickets ---
    try:
        ticket_response = supabase.table('support_tickets').select('id', count='exact').eq('customer_id', customer_id).neq('status', 'Closed').neq('status', 'Resolved').execute()
        if ticket_response.count is not None:
            open_ticket_count = ticket_response.count
    except Exception as e:
        print(f"Error fetching ticket data: {e}")
        has_error = True

    # --- Existing Logic: Invoices ---
    try:
        invoice_response = supabase.table('invoices').select('id', count='exact').eq('customer_id', customer_id).neq('status', 'Paid').execute()
        if invoice_response.count is not None:
            unpaid_invoice_count = invoice_response.count
    except Exception as e:
        print(f"Error fetching invoice data: {e}")
        has_error = True

    # --- Existing Logic: Appointments ---
    try:
        today = datetime.now().isoformat()
        appt_response = supabase.table('appointments').select('id', count='exact')\
            .eq('customer_id', customer_id)\
            .eq('status', 'Scheduled')\
            .gte('start_time', today)\
            .execute()
        if appt_response.count is not None:
            upcoming_appt_count = appt_response.count
    except Exception as e:
        print(f"Error fetching appointment data: {e}")
        has_error = True

    # --- Existing Logic: Network Updates ---
    try:
        query = supabase.table('network_status').select('*')\
            .eq('company_id', user['company_id'])\
            .in_('status_type', ['Outage', 'Maintenance', 'Degraded'])\
            .order('created_at', desc=True).limit(5)
        
        filter_or = "zone_ids.is.null"  
        if customer_zone_id:
            filter_or += f",zone_ids.cs.{{{customer_zone_id}}}"  
        
        query = query.or_(filter_or)
        
        update_response = query.execute()
        if update_response.data:
            network_updates = update_response.data
            
    except Exception as e:
        print(f"Error fetching network updates: {e}")
        has_error = True

    if has_error:
        flash('Could not load all dashboard summary data.', 'error')

    # --- NEW: Fetch Portal Ads ---
    portal_ads = get_portal_ads()

    return render_template('dashboard_overview.html',  
                           open_tickets=open_ticket_count,  
                           unpaid_invoices=unpaid_invoice_count,
                           upcoming_appointments=upcoming_appt_count,
                           network_updates=network_updates,
                           portal_ads=portal_ads) # <-- Added portal_ads  


@app.route('/pay-invoice/<uuid:invoice_id>')
@customer_login_required
def initiate_invoice_payment(invoice_id):
    """
    1. Looks up invoice & company credentials.
    2. Calls bKash Create Payment API.
    3. Redirects user to bKash payment page.
    """
    user = get_user_from_session()
    
    try:
        # 1. Fetch Invoice
        inv_res = supabase.table('invoices').select('*').eq('id', str(invoice_id)).eq('customer_id', user['customer_id']).maybe_single().execute()
        if not inv_res.data:
            flash("Invoice not found.", "error")
            return redirect(url_for('invoices'))
        
        invoice = inv_res.data
        if invoice['status'] == 'Paid':
            flash("This invoice is already paid.", "info")
            return redirect(url_for('invoices'))

        # 2. Fetch Company Credentials (DYNAMIC)
        comp_res = supabase.table('isp_companies').select('payment_gateway_settings').eq('id', invoice['company_id']).maybe_single().execute()
        gateway_settings = comp_res.data.get('payment_gateway_settings', {}).get('bkash', {})
        
        if not gateway_settings.get('enabled'):
            flash("Online payment is not enabled for your ISP. Please contact them.", "error")
            return redirect(url_for('invoices'))

        # 3. Initialize Gateway
        bkash = BkashGateway(
            username=gateway_settings.get('username'),
            password=gateway_settings.get('password'),
            app_key=gateway_settings.get('app_key'),
            app_secret=gateway_settings.get('app_secret'),
            is_sandbox=gateway_settings.get('is_sandbox', True)
        )

        # 4. Create Payment
        token = bkash.get_token()
        callback_url = url_for('bkash_callback', _external=True)
        
        resp = bkash.create_payment(token, invoice['amount'], invoice['invoice_number'], callback_url)
        
        if resp.get('statusCode') == '0000':
            payment_id = resp['paymentID']
            bkash_url = resp['bkashURL']
            
            # 5. Store Payment ID in Invoice for verification later
            supabase.table('invoices').update({'gateway_payment_id': payment_id}).eq('id', invoice['id']).execute()
            
            return redirect(bkash_url)
        else:
            raise Exception(resp.get('statusMessage', 'Unknown bKash Error'))

    except Exception as e:
        print(f"Payment Init Error: {e}")
        flash(f"Payment initiation failed: {e}", "error")
        return redirect(url_for('invoices'))

@app.route('/payment/bkash/callback')
def bkash_callback():
    """
    Handles the redirect back from bKash:
    1. Verifies payment.
    2. Updates Invoice to 'Paid'.
    3. Sends Real-Time Notification to Admin App (WITH CUSTOMER NAME).
    4. Generates PDF Receipt & Emails Customer.
    5. Auto-Reactivates Internet Service.
    """
    payment_id = request.args.get('paymentID')
    status = request.args.get('status')
    
    # 1. Basic Validation
    if not payment_id or status != 'success':
        flash("Payment cancelled or failed.", "error")
        return redirect(url_for('invoices'))

    try:
        # 2. Find Invoice by Payment ID (Fetch customer details for email)
        inv_res = supabase.table('invoices').select('*, customers(*)').eq('gateway_payment_id', payment_id).maybe_single().execute()
        
        # Handle "Double Callback" (if user refreshed or bKash called twice)
        if not inv_res or not inv_res.data:
            return redirect(url_for('invoices'))
        
        invoice = inv_res.data
        customer = invoice.get('customers', {}) # Get customer data
        customer_name = customer.get('full_name', 'Unknown Customer') # Extract Name
        
        # 3. Fetch Company Credentials
        comp_res = supabase.table('isp_companies').select('payment_gateway_settings').eq('id', invoice['company_id']).maybe_single().execute()
        gateway_settings = comp_res.data.get('payment_gateway_settings', {}).get('bkash', {})
        
        bkash = BkashGateway(
            username=gateway_settings.get('username'),
            password=gateway_settings.get('password'),
            app_key=gateway_settings.get('app_key'),
            app_secret=gateway_settings.get('app_secret'),
            is_sandbox=gateway_settings.get('is_sandbox', True)
        )

        # 4. Execute Payment (Verify with bKash API)
        token = bkash.get_token()
        resp = bkash.execute_payment(token, payment_id)
        
        if resp.get('statusCode') == '0000':
            trx_id = resp.get('trxID')
            paid_time_iso = datetime.now().isoformat()
            
            # 5. Update Invoice in Database
            update_payload = {
                'status': 'Paid',
                'paid_at': paid_time_iso,
                'payment_method': 'bKash Online',
                'transaction_id': trx_id,
                'gateway_payment_id': None 
            }
            supabase.table('invoices').update(update_payload).eq('id', invoice['id']).execute()
            
            # --- UPDATE LOCAL OBJECT ---
            invoice['status'] = 'Paid'
            invoice['paid_at'] = paid_time_iso
            invoice['payment_method'] = 'bKash Online'
            invoice['transaction_id'] = trx_id
            
            # 6. TRIGGER ADMIN NOTIFICATION (UPDATED with Customer Name)
            send_admin_notification(
                company_id=invoice['company_id'],
                title="Payment Received",
                # Message now includes the Customer Name
                message=f"Invoice #{invoice['invoice_number']} paid by {customer_name} ({invoice['amount']} BDT).",
                notif_type="Payment",
                related_id=str(invoice['id'])
            )

            # 7. Generate PDF Receipt & Send Email
            try:
                print("Attempting to send receipt email...")
                customer_email = customer.get('email')
                
                if customer_email:
                    company_details = invoice_utils.get_isp_company_details_from_db(invoice['company_id'])
                    
                    pdf_gen = invoice_utils.create_thermal_receipt_as_bytes(
                        invoice,  
                        customer, # Pass full customer dict
                        company_details,  
                        "Online bKash System" 
                    )
                    
                    success, pdf_bytes = pdf_gen
                    if success:
                        pdf_filename = f"receipt_{invoice['invoice_number']}.pdf"
                        with open(pdf_filename, 'wb') as f:
                            f.write(pdf_bytes)
                        
                        email_service.send_invoice_email(
                            customer_email=customer_email,
                            customer_name=customer_name,
                            invoice_data=invoice,
                            company_details=company_details,
                            pdf_attachment_path=pdf_filename
                        )
                        print(f"SUCCESS: Email sent to {customer_email}")
                        
                        if os.path.exists(pdf_filename): os.remove(pdf_filename)
                    else:
                        print(f"PDF Error: {pdf_bytes}") 
            except Exception as e:
                print(f"EMAIL FAILED: {e}")

            # 8. Auto-Reactivate Internet Service
            try:
                reactivate_service(invoice['customer_id'])
                flash("Payment successful! Internet reactivated. Receipt emailed.", "success")
            except Exception:
                flash("Payment successful! Receipt emailed.", "success")
                
            return redirect(url_for('invoices'))
        else:
            flash(f"Payment verification failed: {resp.get('statusMessage')}", "error")
            return redirect(url_for('invoices'))

    except Exception as e:
        print(f"Callback System Error: {e}")
        return redirect(url_for('invoices'))

@app.route('/invoices')
@customer_login_required
def invoices():
     user = get_user_from_session(); invoices = []
     try:
         response = supabase.table('invoices').select('*').eq('customer_id', user['customer_id']).order('issue_date', desc=True).execute()
         if response.data:
             invoices = response.data
             for inv in invoices:
                 inv['issue_date'] = datetime.fromisoformat(inv['issue_date']).date()
                 inv['due_date'] = datetime.fromisoformat(inv['due_date']).date()
                 if isinstance(inv['package_details'], str):
                     try: inv['package_details'] = json.loads(inv['package_details'])
                     except: inv['package_details'] = {'name': 'N/A'}
                 elif inv['package_details'] is None: inv['package_details'] = {'name': 'N/A'}
     except Exception as e: print(f"Error fetching invoices: {e}"); flash('Could not load your invoices.', 'error')

     return render_template('dashboard.html', invoices=invoices)


@app.route('/invoices/<invoice_id>/receipt')
@customer_login_required
def view_receipt(invoice_id):
    user = get_user_from_session()
    if not supabase: abort(500, "Database connection not available")
    
    charge_details = None
    employee_name = None

    try:
        response = supabase.table('invoices').select('*, customers(full_name, email, address, phone_number)').eq('id', invoice_id).eq('customer_id', user['customer_id']).eq('status', 'Paid').maybe_single().execute()
        if not response.data: flash("Receipt not found or invoice is not paid.", 'error'); return redirect(url_for('invoices'))
        
        invoice_data = response.data
        customer_data = invoice_data.get('customers')
        if customer_data is None: flash("Cannot generate receipt: Customer data is missing.", 'error'); return redirect(url_for('invoices'))
        
        pkg_details = invoice_data.get('package_details')
        if isinstance(pkg_details, str):
            try: pkg_details = json.loads(pkg_details)
            except: pkg_details = {}
        
        if isinstance(pkg_details, dict) and "One-Time" in pkg_details.get('name', ''):
            charge_res = supabase.table('one_time_charges').select(
                'charge_details, employees(full_name)'
            ).eq('invoice_id', invoice_id).maybe_single().execute()
            
            if charge_res and charge_res.data:
                charge_details = charge_res.data.get('charge_details')
                if charge_res.data.get('employees'):
                    employee_name = charge_res.data['employees'].get('full_name')
        
        # --- CORRECTED FUNCTION CALL ---
        company_data_for_pdf = invoice_utils.get_isp_company_details_from_db(user['company_id'])
        
        pdf_gen = invoice_utils.create_receipt_pdf_as_bytes(
            invoice_data,  
            customer_data,  
            company_data_for_pdf,  
            "Customer Portal",
            charge_details=charge_details,
            employee_name=employee_name
        )
        
        success, pdf_bytes_or_error = pdf_gen
        if not success: raise Exception(f"PDF generation failed: {pdf_bytes_or_error}")
        
        pdf_bytes = pdf_bytes_or_error
        return send_file(BytesIO(pdf_bytes), mimetype='application/pdf', as_attachment=False, download_name=f"Receipt_{invoice_data['invoice_number']}.pdf")
    
    except Exception as e:  
        print(f"Error generating receipt: {e}");  
        flash(f"Error generating receipt: {e}", 'error');  
        return redirect(url_for('invoices'))

@app.route('/support-tickets')
@customer_login_required
def support_tickets():
    user = get_user_from_session()
    tickets = []
    try:
        response = supabase.table('support_tickets').select(
            'id, ticket_number, subject, status, created_at, employees ( full_name, phone_number )'
        ).eq('customer_id', user['customer_id']).order('created_at', desc=True).execute()
        
        if response.data:
            tickets = response.data
            for t in tickets:  
                t['created_at'] = datetime.fromisoformat(t['created_at']).date()
                
    except Exception as e:  
        print(f"Error fetching tickets: {e}")
        flash("Could not load your support tickets.", 'error')
        
    return render_template('support_tickets.html', tickets=tickets)


# --- CORRECTED TICKET CREATION ROUTE ---
@app.route('/support-tickets/new', methods=['GET', 'POST'])
@customer_login_required
def create_ticket():
    user = get_user_from_session()
    
    if request.method == 'POST':
        subject = request.form.get('subject')
        description = request.form.get('description')
        priority = request.form.get('priority', 'Low') 
        
        customer_id = user['customer_id']
        customer_name = user['customer_name'] 
        company_id = user['company_id']  

        try:
            # 1. Generate Ticket Number
            ticket_num_res = supabase.rpc('generate_new_ticket_number', {'p_company_id': company_id}).execute()
            if not ticket_num_res.data:
                raise Exception("Failed to generate new ticket number.")
            ticket_number = ticket_num_res.data

            # 2. SLA LOGIC: Fetch Company Settings from DB
            comp_res = supabase.table('isp_companies').select('sla_config').eq('id', company_id).single().execute()
            
            # Default to hardcoded if DB config is missing
            default_sla = {"Low": 48, "Medium": 24, "High": 8, "Critical": 2}
            sla_config = comp_res.data.get('sla_config') if comp_res.data else default_sla
            if not sla_config: sla_config = default_sla
            
            # 3. Calculate Deadlines using UTC
            hours = int(sla_config.get(priority, 48))
            now_utc = datetime.now(timezone.utc)
            due_at = (now_utc + timedelta(hours=hours)).isoformat()

            # --- 4. AUTO-ASSIGN LOGIC (New Feature) ---
            assigned_emp_id = None
            assigned_at = None
            
            try:
                # A. Get Customer's Zone
                cust_res = supabase.table('customers').select('zone_id').eq('id', customer_id).single().execute()
                
                if cust_res.data and cust_res.data.get('zone_id'):
                    zone_id = cust_res.data['zone_id']
                    
                    # B. Find Active Employees in that Zone
                    emp_res = supabase.table('employees').select('id, full_name, email')\
                        .eq('zone_id', zone_id)\
                        .eq('status', 'Active').execute()
                    
                    candidates = emp_res.data or []
                    
                    if candidates:
                        # C. Find Employee with FEWEST Open/In-Progress Tickets
                        best_candidate = None
                        min_tickets = float('inf')
                        
                        for emp in candidates:
                            # Count open tickets for this employee
                            count_res = supabase.table('support_tickets').select('id', count='exact')\
                                .eq('assigned_to_employee_id', emp['id'])\
                                .in_('status', ['Open', 'In Progress'])\
                                .execute()
                            
                            count = count_res.count if count_res.count is not None else 0
                            
                            if count < min_tickets:
                                min_tickets = count
                                best_candidate = emp
                        
                        # D. Assign to the best candidate
                        if best_candidate:
                            assigned_emp_id = best_candidate['id']
                            assigned_at = now_utc.isoformat()
                            print(f"Auto-Assigned Ticket {ticket_number} to {best_candidate['full_name']} (Load: {min_tickets})")
                            
                            # E. Send Notification Email to Employee
                            try:
                                # Fetch company details for branding
                                company_details = invoice_utils.get_isp_company_details_from_db(company_id)
                                
                                # Fetch customer details for the email body
                                cust_info_res = supabase.table('customers').select('full_name, phone_number, address').eq('id', customer_id).single().execute()
                                cust_info = cust_info_res.data or {}
                                
                                email_service.send_ticket_assignment_email(
                                    employee_email=best_candidate['email'],
                                    employee_name=best_candidate['full_name'],
                                    ticket_number=ticket_number,
                                    customer=cust_info,
                                    ticket_description=description,
                                    company_details=company_details
                                )
                            except Exception as email_err:
                                print(f"Failed to send auto-assign email: {email_err}")

            except Exception as auto_assign_err:
                print(f"Auto-Assign System Error: {auto_assign_err}")
                # Continue creating ticket even if auto-assign fails
            
            # ------------------------------------------

            # 5. Insert Ticket
            payload = {
                'customer_id': customer_id,
                'company_id': company_id,
                'subject': subject,
                'description': description,
                'status': 'Open',
                'ticket_number': ticket_number,
                'priority': priority,
                'due_at': due_at,
                'assigned_to_employee_id': assigned_emp_id, # Can be None if no match found
                'assigned_at': assigned_at
            }
            
            response = supabase.table('support_tickets').insert(payload).execute()
            
            if response.data:
                new_ticket_id = response.data[0]['id']
                
                # Notifications & Logging
                try:
                    supabase.rpc('log_new_ticket_notification', {
                        'p_company_id': company_id,
                        'p_ticket_id': new_ticket_id,
                        'p_customer_name': customer_name,
                        'p_ticket_subject': subject
                    }).execute()
                except Exception as e: print(f"Notif Error: {e}")
                
                log_portal_action("New Ticket", f"Created Ticket #{ticket_number} ({priority})")
                
                msg = f'Ticket #{ticket_number} created successfully!'
                if assigned_emp_id:
                    msg += " A technician has been automatically assigned."
                
                flash(msg, 'success')
                return redirect(url_for('support_tickets'))
            else:
                flash('There was an error creating your ticket.', 'error')
                
        except Exception as e:
            print(f"Error creating ticket: {e}")
            flash(f'An error occurred: {str(e)}', 'error')
            
        return redirect(url_for('create_ticket'))

    return render_template('create_ticket.html')


@app.route('/ticket/<ticket_id>')
@customer_login_required
def view_ticket(ticket_id):
    user = get_user_from_session()
    ticket = None
    replies = []
    existing_rating = None
    
    if not supabase:  
        flash('Database connection error.', 'error')
        return redirect(url_for('support_tickets'))
        
    try:
        # 1. Fetch Ticket Details (Removed maybe_single for safety)
        response = supabase.table('support_tickets').select(
            'id, ticket_number, subject, description, status, created_at, '
            'employees ( full_name, phone_number )'
        ).eq('id', ticket_id).eq('customer_id', user['customer_id']).execute()
        
        # Defensive Check: Ensure response exists and has data
        if not response or not response.data or len(response.data) == 0:
            flash("Ticket not found.", 'error')
            return redirect(url_for('support_tickets'))
        
        ticket = response.data[0] # Get first item manually
        
        # 2. Fetch Replies
        replies_response = supabase.table('ticket_replies').select(
            'message, created_at'
        ).eq('ticket_id', ticket_id).order('created_at', desc=False).execute()
        
        if replies_response and replies_response.data:
            replies = replies_response.data

        # 3. Check for Existing Rating (Removed maybe_single)
        # We check if list is not empty instead
        rating_res = supabase.table('ticket_ratings').select('*')\
            .eq('ticket_id', ticket_id)\
            .execute()
        
        if rating_res and rating_res.data and len(rating_res.data) > 0:
            existing_rating = rating_res.data[0]
        
        return render_template('ticket_detail.html', 
                               ticket=ticket, 
                               replies=replies, 
                               existing_rating=existing_rating, 
                               datetime=datetime)

    except Exception as e:  
        print(f"Error fetching ticket detail: {e}") 
        # Print full traceback for easier debugging if it happens again
        import traceback
        traceback.print_exc()
        
        flash("Error loading ticket details.", 'error')
        return redirect(url_for('support_tickets'))

@app.route('/ticket/<ticket_id>/rate', methods=['POST'])
@customer_login_required
def rate_ticket(ticket_id):
    user = get_user_from_session()
    
    try:
        rating = int(request.form.get('rating'))
        feedback = request.form.get('feedback')
        
        # 1. Verify ownership & status
        res = supabase.table('support_tickets').select('status, assigned_to_employee_id')\
            .eq('id', ticket_id).eq('customer_id', user['customer_id'])\
            .single().execute()
            
        if not res.data:
            flash("Ticket not found.", "error")
            return redirect(url_for('support_tickets'))
            
        ticket = res.data
        if ticket['status'] != 'Resolved':
            flash("You can only rate resolved tickets.", "warning")
            return redirect(url_for('view_ticket', ticket_id=ticket_id))

        # 2. Check if already rated (Optional, prevents duplicates)
        existing = supabase.table('ticket_ratings').select('id').eq('ticket_id', ticket_id).execute()
        if existing.data:
            flash("You have already rated this ticket.", "info")
            return redirect(url_for('view_ticket', ticket_id=ticket_id))

        # 3. Insert Rating
        payload = {
            'ticket_id': ticket_id,
            'customer_id': user['customer_id'],
            'employee_id': ticket['assigned_to_employee_id'],
            'rating': rating,
            'feedback': feedback
        }
        supabase.table('ticket_ratings').insert(payload).execute()
        
        flash("Thank you for your feedback!", "success")
        return redirect(url_for('view_ticket', ticket_id=ticket_id))
        
    except Exception as e:
        print(f"Error rating ticket: {e}")
        flash("Could not submit rating.", "error")
        return redirect(url_for('view_ticket', ticket_id=ticket_id))

@app.route('/ticket/<ticket_id>/feedback', methods=['GET', 'POST'])
def ticket_feedback(ticket_id):
    """Allows customer to rate the service after resolution."""
    # Note: We keep this public or token-based so they can rate from email without logging in easily.
    # For security, you can verify a token or require login. For now, checking DB existence.
    
    if request.method == 'POST':
        try:
            rating = int(request.form.get('rating'))
            comment = request.form.get('comment')
            
            # 1. Fetch ticket to get IDs
            t_res = supabase.table('support_tickets').select('customer_id, assigned_to_employee_id').eq('id', ticket_id).single().execute()
            if not t_res.data:
                flash("Ticket not found.", "error")
                return redirect(url_for('index'))
                
            ticket = t_res.data
            
            # 2. Save Rating
            payload = {
                'ticket_id': ticket_id,
                'customer_id': ticket['customer_id'],
                'employee_id': ticket['assigned_to_employee_id'],
                'rating': rating,
                'feedback': comment
            }
            supabase.table('ticket_ratings').insert(payload).execute()
            
            return render_template('feedback_success.html')
            
        except Exception as e:
            print(f"Rating Error: {e}")
            flash("An error occurred while saving your feedback.", "error")
    
    # GET Request: Show form
    try:
        # Only allow rating if Resolved/Closed
        res = supabase.table('support_tickets').select('*').eq('id', ticket_id).single().execute()
        if not res.data: return "Ticket not found."
        ticket = res.data
        
        if ticket['status'] not in ['Resolved', 'Closed']:
            flash("This ticket is not yet resolved.", "info")
            return redirect(url_for('view_ticket', ticket_id=ticket_id))
            
        return render_template('ticket_feedback.html', ticket=ticket)
        
    except Exception as e:
        return f"Error: {e}"

@app.route('/upload-avatar', methods=['POST'])
@login_required
def upload_avatar():
    user = get_user_from_session()
    if 'avatar_file' not in request.files:
        flash('No file part', 'error')
        return redirect(url_for('profile'))
        
    file = request.files['avatar_file']
    if file.filename == '':
        flash('No selected file', 'error')
        return redirect(url_for('profile'))

    if file:
        try:
            img = Image.open(file.stream)
            
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
                
            img_buffer = io.BytesIO()
            
            img.save(img_buffer, "JPEG", quality=90, optimize=True)
            
            quality = 90
            while img_buffer.tell() > 200000 and quality > 10:
                print(f"Image is {img_buffer.tell()} bytes. Compressing further...")
                img_buffer.seek(0)  
                img_buffer.truncate(0)  
                quality -= 10  
                img.save(img_buffer, "JPEG", quality=quality, optimize=True)
            
            if img_buffer.tell() > 200000:
                flash('Image is too large (over 200KB), even after compression.', 'error')
                return redirect(url_for('profile'))

            print(f"Final image size: {img_buffer.tell()} bytes")
            img_buffer.seek(0)
            
            user_id = user['auth_id']
            file_path = f"public/{user_id}.jpg"  
            
            supabase.storage.from_('avatars').upload(
                file=img_buffer.read(),
                path=file_path,
                file_options={"content-type": "image/jpeg", "upsert": "true"}
            )
            
            public_url = supabase.storage.from_('avatars').get_public_url(file_path)
            
            table_to_update = 'employees' if user.get('is_employee') else 'customers'
            record_id = user.get('employee_id') if user.get('is_employee') else user.get('customer_id')
            
            supabase.table(table_to_update).update(
                {'profile_avatar_url': public_url}
            ).eq('id', record_id).execute()
            
            session['user']['avatar_url'] = public_url
            session.modified = True
            
            flash('Profile picture updated!', 'success')
            
        except Exception as e:
            print(f"Error uploading avatar: {e}")
            flash(f"Error uploading image: {e}", 'error')

    return redirect(url_for('profile'))



# Helper to generate company prefix (e.g. "Green ISP" -> "GRE")
def get_company_prefix(name):
    if not name: return "EMP"
    return name[:3].upper()

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user' not in session:
        return redirect(url_for('login'))

    user_email = session['user']['email']
    is_employee = session['user'].get('is_employee', False)

    # --- Handle Password Update (POST) ---
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if new_password != confirm_password:
            flash("New passwords do not match.", "error")
        else:
            try:
                res = supabase.auth.update_user({"password": new_password})
                if res.user:
                    flash("Password updated successfully!", "success")
                else:
                    flash("Failed to update password.", "error")
            except Exception as e:
                flash(f"Error: {str(e)}", "error")
        return redirect(url_for('profile'))

    # --- Fetch Profile Data (GET) ---
    employee_details = {}
    package_info = {}
    smart_id = "N/A"
    avg_rating = 0.0
    review_count = 0

    try:
        if is_employee:
            # 1. Fetch Employee + Company + Employee Roles
            response = supabase.table('employees')\
                .select('*, isp_companies(company_name), employee_roles(*)')\
                .eq('email', user_email)\
                .maybe_single()\
                .execute()
            
            if response.data:
                employee_details = response.data
                
                # --- A. FIX AVATAR ---
                raw_photo = employee_details.get('profile_avatar_url')
                if raw_photo:
                    if raw_photo.startswith('http'):
                        employee_details['real_photo_url'] = raw_photo
                    else:
                        try:
                            bucket_name = 'avatars' 
                            clean_path = raw_photo.lstrip('/')
                            public_url = supabase.storage.from_(bucket_name).get_public_url(clean_path)
                            employee_details['real_photo_url'] = public_url
                        except Exception as img_err:
                            print(f"Image Error: {img_err}")
                            employee_details['real_photo_url'] = None
                
                # --- B. FIX ROLE ---
                role_data = employee_details.get('employee_roles')
                if role_data:
                    real_role = (role_data.get('role_name') or role_data.get('name') or role_data.get('title'))
                    employee_details['display_role'] = real_role
                else:
                    employee_details['display_role'] = employee_details.get('designation') or 'Staff'

                # --- C. FIX SMART ID ---
                try:
                    cid = employee_details.get('company_id')
                    created_at = employee_details.get('created_at')
                    comp_name = employee_details.get('isp_companies', {}).get('company_name', 'EMP')
                    prefix = get_company_prefix(comp_name)

                    if cid and created_at:
                        count_res = supabase.table('employees').select('id', count='exact').eq('company_id', cid).lte('created_at', created_at).execute()
                        rank = count_res.count if count_res.count else 0
                        smart_id = f"{prefix}-{rank:03d}"
                    else:
                        smart_id = f"{prefix}-000"
                except:
                    smart_id = "EMP-???"

                # --- D. NEW: CALCULATE RATINGS ---
                try:
                    rating_res = supabase.table('ticket_ratings').select('rating').eq('employee_id', employee_details['id']).execute()
                    ratings_data = rating_res.data or []
                    review_count = len(ratings_data)
                    
                    if review_count > 0:
                        total_stars = sum(r['rating'] for r in ratings_data)
                        avg_rating = round(total_stars / review_count, 1)
                except Exception as e:
                    print(f"Rating Calc Error: {e}")

        else:
            # Customer Logic
            cust_res = supabase.table('customers').select('*').eq('email', user_email).maybe_single().execute()
            if cust_res.data:
                smart_id = f"CUST-{cust_res.data.get('id', '000')}"
                
                pkg_id = cust_res.data.get('package_id')
                if pkg_id:
                    pkg_res = supabase.table('packages').select('*').eq('id', pkg_id).maybe_single().execute()
                    if pkg_res.data:
                        package_info = pkg_res.data

    except Exception as e:
        print(f"Profile Main Error: {e}")
        flash("Could not load profile data.", "error")

    return render_template(
        'profile.html',
        employee_details=employee_details,
        package=package_info,
        smart_id=smart_id,
        avg_rating=avg_rating,      # Passed to template
        review_count=review_count   # Passed to template
    )

# --- EMPLOYEE ROUTES ---

@app.route('/ad-click/<string:ad_id>')
def track_ad_click(ad_id):
    """
    Tracks a click for a specific ad ID and redirects to the destination.
    """
    try:
        # 1. Fetch ads using the helper (so IDs match!)
        ads = get_portal_ads()
        
        # 2. Find the matching ad
        target_ad = next((ad for ad in ads if ad.get('id') == ad_id), None)
        
        if not target_ad:
            # print(f"Ad Click: ID {ad_id} not found.")
            return redirect(url_for('dashboard_overview')) 
            
        redirect_url = target_ad.get('redirect_url')
        if not redirect_url:
            return redirect(url_for('dashboard_overview'))
            
        # 3. Increment Click Count (Using NEW Daily Logic)
        try:
            supabase.rpc('track_ad_click_daily', {'p_ad_id': ad_id}).execute()
        except Exception as e:
            print(f"Ad Tracking Error: {e}")
            
        # 4. Redirect User
        return redirect(redirect_url)
        
    except Exception as e:
        print(f"Ad Click Route Error: {e}")
        return redirect(url_for('dashboard_overview'))

# --- ADD THIS TO app.py ---

@app.context_processor
def inject_whatsapp_support():
    """
    Fetches the ISP phone number for the floating WhatsApp button.
    """
    user = get_user_from_session()
    support_phone = None

    if user and 'company_id' in user:
        try:
            # FIX: Changed 'phone' to 'contact_phone'
            res = supabase.table('isp_companies').select('contact_phone')\
                .eq('id', user['company_id'])\
                .limit(1).execute()
            
            if res.data and len(res.data) > 0:
                # FIX: Access 'contact_phone' from the result
                raw_phone = res.data[0].get('contact_phone')
                if raw_phone:
                    # Clean the number (remove + - ( ) spaces)
                    support_phone = ''.join(filter(str.isdigit, str(raw_phone)))
        except Exception as e:
            # Fail silently
            pass

    return dict(current_isp_whatsapp=support_phone)

# --- MISSING ROUTES (Required for base.html links) ---

@app.route('/my-plan')
@customer_login_required
def my_plan():
    """
    Shows the customer's current plan and other available packages.
    """
    user = get_user_from_session()
    customer_data = None
    available_packages = []
    
    try:
        # 1. Fetch Current Customer Data (with linked Package details)
        cust_res = supabase.table('customers').select('*, packages(*)').eq('id', user['customer_id']).maybe_single().execute()
        customer_data = cust_res.data
        
        # 2. Fetch All Available Packages for this ISP
        pkg_res = supabase.table('packages').select('*')\
            .eq('company_id', user['company_id'])\
            .order('price', desc=False)\
            .execute()
        available_packages = pkg_res.data or []
        
    except Exception as e:
        print(f"Error fetching plan data: {e}")
        flash("Could not load plan details.", "error")
        
    return render_template('my_plan.html', 
                           customer=customer_data, 
                           packages=available_packages)

@app.route('/my-orders')
@customer_login_required
def my_orders():
    """Shows product order history with pagination."""
    user = get_user_from_session()
    
    # 1. Pagination Params
    page = request.args.get('page', 1, type=int)
    per_page = 10
    start = (page - 1) * per_page
    end = start + per_page - 1
    
    orders = []
    total_count = 0
    
    try:
        # 2. Fetch Orders with Count and Range
        res = supabase.table('product_orders').select('*', count='exact')\
            .eq('customer_details->>email', user['email'])\
            .order('created_at', desc=True)\
            .range(start, end)\
            .execute()
        
        orders = res.data or []
        total_count = res.count or 0
        
    except Exception as e:
        print(f"Error fetching product orders: {e}")
        flash("Could not load order history.", "error")
    
    # 3. Calculate Total Pages
    total_pages = (total_count + per_page - 1) // per_page
        
    return render_template('my_orders.html', 
                           orders=orders, 
                           page=page, 
                           total_pages=total_pages)

@app.route('/billing-history')
@customer_login_required
def billing_history():
    """Redirects to the invoices page."""
    return redirect(url_for('invoices'))

@app.route('/employee/dashboard')
@employee_login_required
def employee_dashboard():
    user = get_user_from_session()
    
    # Initialize variables
    upcoming_appt_count = 0
    total_assigned = 0
    pending = 0
    resolved = 0
    avg_rating = 0.0
    review_count = 0
    avg_resolution_time_str = "N/A" # New Variable
    
    try:
        # 1. Fetch Upcoming Appointments
        today = datetime.now().isoformat()
        appt_response = supabase.table('appointments').select('id', count='exact')\
            .eq('employee_id', user['employee_id'])\
            .eq('status', 'Scheduled')\
            .gte('start_time', today)\
            .execute()
        if appt_response.count is not None:
            upcoming_appt_count = appt_response.count

        # 2. Fetch Ticket Statistics (Modified to get timestamps for math)
        tickets_res = supabase.table('support_tickets').select('status, created_at, closed_at')\
            .eq('assigned_to_employee_id', user['employee_id']).execute()
        
        all_tickets = tickets_res.data or []
        total_assigned = len(all_tickets)
        
        # Count Stats
        pending = sum(1 for t in all_tickets if t['status'] in ['Open', 'In Progress'])
        resolved_tickets = [t for t in all_tickets if t['status'] == 'Resolved']
        resolved = len(resolved_tickets)

        # --- CALCULATE AVERAGE RESOLUTION TIME ---
        if resolved > 0:
            total_seconds = 0
            count_valid = 0
            for t in resolved_tickets:
                if t.get('created_at') and t.get('closed_at'):
                    try:
                        # Handle ISO format. Replace Z with +00:00 for Python compatibility
                        start = datetime.fromisoformat(t['created_at'].replace('Z', '+00:00'))
                        end = datetime.fromisoformat(t['closed_at'].replace('Z', '+00:00'))
                        duration = (end - start).total_seconds()
                        if duration > 0:
                            total_seconds += duration
                            count_valid += 1
                    except:
                        continue
            
            if count_valid > 0:
                avg_sec = total_seconds / count_valid
                # Format logic
                if avg_sec < 3600: # Less than 1 hour
                    avg_resolution_time_str = f"{int(avg_sec // 60)}m"
                elif avg_sec < 86400: # Less than 1 day
                    hours = int(avg_sec // 3600)
                    mins = int((avg_sec % 3600) // 60)
                    avg_resolution_time_str = f"{hours}h {mins}m"
                else: # Days
                    days = int(avg_sec // 86400)
                    hours = int((avg_sec % 86400) // 3600)
                    avg_resolution_time_str = f"{days}d {hours}h"

        # 3. Fetch & Calculate Ratings
        rating_res = supabase.table('ticket_ratings').select('rating')\
            .eq('employee_id', user['employee_id']).execute()
        
        ratings_data = rating_res.data or []
        review_count = len(ratings_data)
        
        if review_count > 0:
            total_stars = sum(r['rating'] for r in ratings_data)
            avg_rating = round(total_stars / review_count, 1)

    except Exception as e:
        print(f"Error fetching employee dashboard data: {e}")
    
    # Fetch Portal Ads
    portal_ads = get_portal_ads()
    
    return render_template('employee_dashboard.html', 
                           upcoming_appointments=upcoming_appt_count,
                           total_assigned=total_assigned,
                           pending=pending,
                           resolved=resolved,
                           avg_rating=avg_rating,
                           review_count=review_count,
                           avg_resolution_time=avg_resolution_time_str, # Passed to template
                           portal_ads=portal_ads)

# --- NEW ROUTE: Employee Add Customer ---
@app.route('/employee/add-customer', methods=['GET', 'POST'])
@employee_login_required
def employee_add_customer():
    user = get_user_from_session()
    
    if request.method == 'POST':
        try:
            email = request.form.get('email', '').strip() or None
            
            data = {
                'company_id': user['company_id'],
                'full_name': request.form['full_name'],
                'phone_number': request.form['phone_number'],
                'email': email,
                'nid_number': request.form.get('nid_number') or None,
                'address': request.form['address'],
                'zone_id': request.form['zone_id'],
                'package_id': request.form['package_id'],
                'latitude': float(request.form['latitude']) if request.form.get('latitude') else None,
                'longitude': float(request.form['longitude']) if request.form.get('longitude') else None,
                'status': 'Pending Activation',
                'added_by_employee_id': user['employee_id'] 
            }
            
            cust_res = supabase.table('customers').insert(data).execute()
            if not cust_res.data: raise Exception("Database Insert Failed")
            new_cust_id = cust_res.data[0]['id']
            
            # Inventory Logic
            inventory_ids = request.form.getlist('inventory_ids[]')
            if inventory_ids:
                supabase.table('inventory_items').update({
                    'customer_id': new_cust_id,
                    'status': 'Assigned' 
                }).in_('id', inventory_ids).execute()
            
            # One-Time Cost
            one_time_cost = float(request.form.get('one_time_cost', 0))
            if one_time_cost > 0:
                cost_desc = request.form.get('cost_description', 'Installation Fee')
                charge_payload = {
                    'company_id': user['company_id'],
                    'customer_id': new_cust_id,
                    'total_amount': one_time_cost,  
                    'charge_details': [{'item': cost_desc, 'cost': one_time_cost}],
                    'status': 'Pending',
                    'employee_id': user['employee_id'] 
                }
                supabase.table('one_time_charges').insert(charge_payload).execute()

            flash(f"Customer '{data['full_name']}' added successfully!", "success")
            log_portal_action("Add Customer", f"Employee added customer: {data['full_name']}")
            return redirect(url_for('employee_dashboard'))

        except Exception as e:
            error_msg = str(e)
            if "23505" in error_msg:
                if "email" in error_msg.lower(): flash("Failed: Email already registered.", "error")
                elif "phone" in error_msg.lower(): flash("Failed: Phone number already registered.", "error")
                else: flash("Failed: Duplicate record.", "error")
            else:
                flash(f"Error: {error_msg}", "error")

    try:
        zones = supabase.table('zones').select('id, name').eq('company_id', user['company_id']).execute().data or []
        packages = supabase.table('packages').select('id, name, price').eq('company_id', user['company_id']).execute().data or []
    except: zones, packages = [], []

    return render_template('employee_add_customer.html', zones=zones, packages=packages)
# --- NEW API ROUTE FOR SCANNER ---
@app.route('/api/inventory/details/<string:qr_code>')
@employee_login_required
def get_inventory_details(qr_code):
    """Fetches model and serial for a scanned QR code."""
    try:
        res = supabase.table('inventory_items').select('model_name, serial_number, status')\
            .eq('qr_code_id', qr_code).maybe_single().execute()
        
        if res.data:
            item = res.data
            if item['status'] != 'In Stock':
                return jsonify({'error': f"Item is currently {item['status']}"}), 400
            return jsonify({
                'model_name': item['model_name'],
                'serial_number': item['serial_number']
            })
        return jsonify({'error': 'Item not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- NEW API: Check Inventory Item Status ---
@app.route('/api/check-inventory', methods=['POST'])
@employee_login_required
def check_inventory_item():
    data = request.get_json()
    qr_code = data.get('qr_code')
    
    if not qr_code:
        return jsonify({'success': False, 'message': 'No QR code provided'})

    try:
        # Check if item exists
        res = supabase.table('inventory_items').select('*').eq('qr_code_id', qr_code).execute()
        
        if not res.data:
            return jsonify({'success': False, 'message': 'Item not found in Inventory system.'})
        
        item = res.data[0]
        
        # Check Status
        if item['status'] != 'In Stock':
            return jsonify({'success': False, 'message': f'Item is currently {item["status"]}. Cannot assign.'})

        # Return item details (Front-end will force S/N confirmation)
        return jsonify({
            'success': True,
            'item': {
                'id': item['id'],
                'model_name': item['model_name'],
                'serial_number': item.get('serial_number') or '', # Return empty string if None
                'qr_code': item['qr_code_id']
            }
        })

    except Exception as e:
        print(f"Inventory Check Error: {e}")
        return jsonify({'success': False, 'message': str(e)})


# --- 2. UPDATE SERIAL NUMBER API ---
@app.route('/api/update-inventory-sn', methods=['POST'])
@employee_login_required
def update_inventory_sn():
    data = request.get_json()
    item_id = data.get('item_id')
    new_sn = data.get('serial_number', '').strip()

    if not item_id or not new_sn:
        return jsonify({'success': False, 'message': 'Serial Number is required.'})

    try:
        # Check if SN is used by ANOTHER item (ignoring self)
        dup = supabase.table('inventory_items').select('id')\
            .eq('serial_number', new_sn)\
            .neq('id', item_id)\
            .execute()
            
        if dup.data:
            return jsonify({'success': False, 'message': 'This Serial Number is already assigned to another device.'})

        # Update
        res = supabase.table('inventory_items').update({'serial_number': new_sn}).eq('id', item_id).execute()
        
        if res.data:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'message': 'Update failed.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/employee/payslips')
@employee_login_required
def employee_payslips():
    user = get_user_from_session()
    payslips = []
    try:
        response = supabase.table('payroll_records') \
            .select('*') \
            .eq('employee_id', user['employee_id']) \
            .eq('status', 'Paid') \
            .order('pay_period_year', desc=True) \
            .order('pay_period_month', desc=True) \
            .execute()
        
        if response.data:
            payslips = response.data
            
    except Exception as e:
        print(f"Error fetching payslips: {e}")
        flash('Could not load your payslips.', 'error')
    
    return render_template('employee_payslips.html', payslips=payslips, datetime=datetime, date=date)

@app.route('/employee/payslip/<record_id>/pdf')
@employee_login_required
def download_payslip(record_id):
    user = get_user_from_session()
    if not supabase: abort(500, "Database connection not available")

    try:
        response = supabase.table('payroll_records').select('*').eq('id', record_id).eq('employee_id', user['employee_id']).maybe_single().execute()
        if not response.data:
            flash("Payslip not found or you do not have permission to view it.", 'error')
            return redirect(url_for('employee_payslips'))
        
        payroll_data = response.data
        
        employee_data = {
            "full_name": user.get('employee_name'),
            "email": user.get('email'),
            "role": user.get('role')
        }
        
        # --- *** CORRECTED FUNCTION CALL *** ---
        company_data = invoice_utils.get_isp_company_details_from_db(user['company_id'])
        
        pdf_gen = invoice_utils.create_payslip_pdf_as_bytes(
            payroll_data,
            employee_data,
            company_data,
            "Employee Portal"
        )
        
        success, pdf_bytes_or_error = pdf_gen
        if not success:  
            raise Exception(f"PDF generation failed: {pdf_bytes_or_error}")
        
        pdf_bytes = pdf_bytes_or_error
        
        pay_period = f"{date(payroll_data['pay_period_year'], payroll_data['pay_period_month'], 1).strftime('%B-%Y')}"
        download_name = f"Payslip-{pay_period}.pdf"
        
        return send_file(
            BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=download_name
        )

    except Exception as e:
        print(f"Error generating payslip: {e}")
        flash(f"Error generating payslip: {e}", 'error')
        return redirect(url_for('employee_payslips'))


@app.route('/employee/expenses', methods=['GET', 'POST'])
@employee_login_required
def employee_expenses():
    user = get_user_from_session()
    
    if request.method == 'POST':
        try:
            payload = {
                "company_id": user['company_id'],
                "employee_id": user['employee_id'],
                "title": request.form['title'],
                "description": request.form['description'],
                "amount": float(request.form['amount']),
                "expense_date": request.form['expense_date'],
                "category_id": request.form.get('category_id') or None,
                "customer_id": request.form.get('customer_id') or None,
                "status": "Pending"  
            }
            
            response = supabase.table('expenses').insert(payload).execute()
            if response.data:
                flash('Expense submitted for approval!', 'success')
            else:
                flash(f"Error submitting expense: {response.error.message if response.error else 'Unknown error'}", 'error')
        except Exception as e:
            print(f"Error submitting expense: {e}")
            flash(f"An error occurred: {e}", 'error')
        return redirect(url_for('employee_expenses'))

    categories = []
    customers = []
    expenses = []
    try:
        cat_res = supabase.table('expense_categories').select('id, name').eq('company_id', user['company_id']).order('name').execute()
        if cat_res.data:
            categories = cat_res.data
            
        cust_res = supabase.table('customers').select('id, full_name').eq('company_id', user['company_id']).eq('status', 'Active').order('full_name').execute()
        if cust_res.data:
            customers = cust_res.data
            
        exp_res = supabase.table('expenses').select(
            '*, expense_categories(name), customers(full_name)'
        ).eq('employee_id', user['employee_id']).order('expense_date', desc=True).execute()
        if exp_res.data:
            expenses = exp_res.data

    except Exception as e:
        print(f"Error loading expense data: {e}")
        flash('Could not load all expense data.', 'error')
        
    return render_template('employee_expenses.html',  
                           categories=categories,  
                           customers=customers,  
                           expenses=expenses,
                           date=date,
                           datetime=datetime)

@app.route('/appointment/<uuid:appointment_id>/complete', methods=['POST'])
@employee_login_required
def complete_appointment(appointment_id):
    user = get_user_from_session()
    
    try:
        # 1. Verify Appointment Ownership & Status
        appt_res = supabase.table('appointments').select('*')\
            .eq('id', str(appointment_id))\
            .eq('employee_id', user['employee_id'])\
            .eq('status', 'Scheduled')\
            .maybe_single().execute()
            
        # FIX: Check if 'appt_res' itself is None before checking '.data'
        if not appt_res or not appt_res.data:
            flash("Appointment not found or already completed.", "error")
            return redirect(url_for('my_appointments'))
        
        # 2. Update Status to 'Completed'
        # Note: We keep it simple (Status only) to avoid schema errors
        update_payload = {
            "status": "Completed"
        }
        
        supabase.table('appointments').update(update_payload).eq('id', str(appointment_id)).execute()
        
        # 3. Log Action
        log_portal_action("Appointment Completed", f"Employee marked appointment {appointment_id} as completed.")
        
        flash("Appointment marked as completed successfully!", "success")
        
    except Exception as e:
        print(f"Error completing appointment: {e}")
        flash(f"An error occurred: {e}", "error")
        
    return redirect(url_for('my_appointments'))


@app.route('/employee/attendance', methods=['GET', 'POST'])
@employee_login_required
def employee_attendance():
    user = get_user_from_session()
    
    today = datetime.now()
    selected_month = request.form.get('month', today.month, type=int)
    selected_year = request.form.get('year', today.year, type=int)

    start_date = f"{selected_year}-{selected_month:02d}-01"
    next_month_date = (date(selected_year, selected_month, 1) + timedelta(days=32)).replace(day=1)
    end_date = (next_month_date - timedelta(days=1)).strftime("%Y-%m-%d")

    attendance_records = []
    try:
        response = supabase.table('attendance').select('*') \
            .eq('employee_id', user['employee_id']) \
            .gte('date', start_date) \
            .lte('date', end_date) \
            .order('date', desc=True) \
            .execute()
        
        if response.data:
            attendance_records = response.data
            
    except Exception as e:
        print(f"Error fetching attendance: {e}")
        flash('Could not load your attendance records.', 'error')

    return render_template('employee_attendance.html',
                           attendance_records=attendance_records,
                           selected_month=selected_month,
                           selected_year=selected_year,
                           datetime=datetime,
                           date=date)


@app.route('/employee/my-tickets')
@employee_login_required
def employee_my_tickets():
    user = get_user_from_session()
    tickets = []
    try:
        response = supabase.table('support_tickets').select(
            '*, customers(full_name, phone_number, address)'
        ).eq('assigned_to_employee_id', user['employee_id'])\
         .neq('status', 'Closed')\
         .order('created_at', desc=True).execute()
        
        if response.data:
            tickets = response.data
            
    except Exception as e:
        print(f"Error fetching assigned tickets: {e}")
        flash('Could not load your assigned tickets.', 'error')

    return render_template('employee_my_tickets.html', tickets=tickets, datetime=datetime)


@app.route('/employee/ticket/<ticket_id>', methods=['GET', 'POST'])
@employee_login_required
def employee_view_ticket(ticket_id):
    user = get_user_from_session()
    
    # --- HANDLE STATUS UPDATES ---
    if request.method == 'POST':
        new_status = request.form.get('status')
        if new_status:
            try:
                # Fetch current data to check existing timestamps
                ticket_check = supabase.table('support_tickets').select(
                    'ticket_number, subject, assigned_at, customers(full_name, email)'
                ).eq('id', ticket_id).maybe_single().execute()
                
                if not ticket_check.data:
                    flash("Ticket not found.", "error")
                    return redirect(url_for('employee_my_tickets'))
                
                current_data = ticket_check.data
                ticket_num = current_data.get('ticket_number')
                customer_info = current_data.get('customers', {})
                
                # Update Payload
                update_payload = {'status': new_status}
                
                # --- TIMESTAMP LOGIC (UTC) ---
                if new_status == 'Resolved':
                    update_payload['resolved_at'] = datetime.now(timezone.utc).isoformat()
                
                if new_status == 'In Progress' and not current_data.get('assigned_at'):
                    update_payload['assigned_at'] = datetime.now(timezone.utc).isoformat()
                # -----------------------------

                # Execute Update
                response = supabase.table('support_tickets').update(update_payload)\
                    .eq('id', ticket_id)\
                    .eq('assigned_to_employee_id', user['employee_id'])\
                    .execute()
                
                if response.data:
                    flash(f'Ticket updated to {new_status}', 'success')
                    
                    # --- SAFE BACKGROUND EMAIL TASK ---
                    if new_status in ['Resolved', 'Closed'] and customer_info.get('email'):
                        
                        def send_ticket_email_safe(c_details, c_email, c_name, t_num, t_sub, n_stat, t_id):
                            print(f"--- [BACKGROUND] Sending Ticket Email for {t_num} ---")
                            try:
                                email_service.send_ticket_status_update_email(
                                    customer_email=c_email,
                                    customer_name=c_name,
                                    ticket_number=t_num,
                                    ticket_subject=t_sub,
                                    new_status=n_stat,
                                    company_details=c_details,
                                    ticket_id=t_id 
                                )
                                print("--- Ticket Email Sent ---")
                            except Exception as e:
                                print(f"Ticket Email Error: {e}")

                        try:
                            # Prepare Data
                            company_details = invoice_utils.get_isp_company_details_from_db(user['company_id'])
                            
                            # USE EXECUTOR (Prevents Crash)
                            executor.submit(
                                send_ticket_email_safe,
                                company_details, 
                                customer_info.get('email'), 
                                customer_info.get('full_name'), 
                                ticket_num, 
                                current_data.get('subject'), 
                                new_status, 
                                ticket_id
                            )
                            
                            flash('Customer notification queued.', 'info')
                        except Exception as e:
                            print(f"Failed to queue email: {e}")
                    # ----------------------------------

                else:
                    flash('Could not update status. Verify assignment.', 'error')
                    
            except Exception as e:
                print(f"Error updating ticket: {e}")
                flash(f'Error: {e}', 'error')
        return redirect(url_for('employee_view_ticket', ticket_id=ticket_id))

    # --- VIEW PAGE LOGIC ---
    ticket = None
    replies = []
    try:
        # Fetch ticket + lat/long for map
        response = supabase.table('support_tickets').select(
            '*, customers(full_name, phone_number, address, latitude, longitude)'
        ).eq('id', ticket_id)\
         .eq('assigned_to_employee_id', user['employee_id'])\
         .maybe_single().execute()
        
        if not response.data:
            flash("Ticket not found or not assigned to you.", 'error')
            return redirect(url_for('employee_my_tickets'))
        
        ticket = response.data
        
        # Fetch Replies
        replies_response = supabase.table('ticket_replies').select(
            'message, created_at'
        ).eq('ticket_id', ticket_id).order('created_at', desc=False).execute()
        
        if replies_response.data:
            replies = replies_response.data
            
    except Exception as e:
        print(f"Error fetching ticket details: {e}")
        flash('Error loading ticket details.', 'error')
        return redirect(url_for('employee_my_tickets'))
        
    return render_template('employee_ticket_detail.html', ticket=ticket, replies=replies, datetime=datetime)

@app.route('/employee/leave-requests', methods=['GET', 'POST'])
@employee_login_required
def employee_leave_requests():
    user = get_user_from_session()
    
    if request.method == 'POST':
        try:
            start_date = request.form['start_date']
            end_date = request.form['end_date']
            reason = request.form['reason']  

            if not all([start_date, end_date, reason]):
                flash('All fields are required.', 'error')
                return redirect(url_for('employee_leave_requests'))
            
            if end_date < start_date:
                flash('End date cannot be before start date.', 'error')
                return redirect(url_for('employee_leave_requests'))

            payload = {
                "company_id": user['company_id'],
                "employee_id": user['employee_id'],
                "start_date": start_date,
                "end_date": end_date,
                "reason": reason,
                "status": "Pending"
            }
            
            response = supabase.table('leave_requests').insert(payload).execute()
            if response.data:
                flash('Leave request submitted successfully!', 'success')
                log_portal_action("Leave Request", f"Submitted leave request for {start_date} to {end_date}")
            else:
                flash(f"Error submitting request: {response.error.message if response.error else 'Unknown error'}", 'error')
        except Exception as e:
            print(f"Error submitting leave request: {e}")
            flash(f"An error occurred: {e}", 'error')
        return redirect(url_for('employee_leave_requests'))

    leave_requests = []
    try:
        response = supabase.table('leave_requests').select('*') \
            .eq('employee_id', user['employee_id']) \
            .order('requested_at', desc=True) \
            .execute()
        
        if response.data:
            leave_requests = response.data
            
    except Exception as e:
        print(f"Error loading leave requests: {e}")
        flash('Could not load your leave request history.', 'error')
        
    return render_template('employee_leave_requests.html',  
                           leave_requests=leave_requests,
                           date=date,
                           datetime=datetime)

@app.route('/my-appointments')
@login_required
def my_appointments():
    user = get_user_from_session()
    
    try:
        # 1. Build Query (Fetch ALL statuses)
        query = supabase.table('appointments').select(
            '*, customers(full_name, phone_number), employees(full_name)'
        ).eq('company_id', user['company_id'])
        
        # Filter by User Role
        if user.get('is_employee'):
            query = query.eq('employee_id', user['employee_id'])
        else:
            query = query.eq('customer_id', user.get('id') or user.get('customer_id'))
            
        # Order by Date (Newest First)
        res = query.order('start_time', desc=True).execute()
        appointments = res.data or []
        
        # 2. Calculate Stats (Live from Database)
        stats = {
            'total': len(appointments),
            'upcoming': sum(1 for a in appointments if a['status'] == 'Scheduled'),
            'completed': sum(1 for a in appointments if a['status'] == 'Completed'),
            'canceled': sum(1 for a in appointments if a['status'] == 'Canceled')
        }
        
        # 3. Render Template (PASS 'datetime' HERE)
        return render_template(
            'my_appointments.html', 
            appointments=appointments, 
            stats=stats, 
            datetime=datetime  # <--- CRITICAL FIX
        )

    except Exception as e:
        print(f"Error fetching appointments: {e}")
        # Pass empty data + datetime to prevent template crash on error page too
        return render_template(
            'my_appointments.html', 
            appointments=[], 
            stats={'total':0, 'upcoming':0, 'completed':0}, 
            datetime=datetime
        )

@app.route('/knowledge-base')
@customer_login_required
def knowledge_base():
    user = get_user_from_session()
    categories = {}
    try:
        articles_res = supabase.table('kb_articles').select('*')\
            .eq('company_id', user['company_id'])\
            .eq('status', 'Published')\
            .order('category').order('title')\
            .execute()
        
        if articles_res.data:
            for article in articles_res.data:
                cat = article.get('category', 'General')
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(article)
                
    except Exception as e:
        print(f"Error fetching knowledge base: {e}")
        flash('Could not load help articles.', 'error')
        
    return render_template('knowledge_base.html', categories=categories)


# --- *** BILL COLLECTION ROUTES *** ---
@app.route('/employee/billing')
@permission_required('can_manage_billing')
def employee_billing():
    user = get_user_from_session()
    invoices = []
    zones = []
    
    search_term = request.args.get('search', '').strip()
    selected_zone = request.args.get('zone', '')
    
    try:
        # Fetch Zones
        zone_res = supabase.table('zones').select('id, name')\
            .eq('company_id', user['company_id']).order('name').execute()
        if zone_res.data:
            zones = zone_res.data

        # Base Invoice Query
        query = supabase.table('invoices').select(
            '*, customers!inner(full_name, phone_number, address, zone_id, zones(name))'
        ).eq('company_id', user['company_id'])\
         .in_('status', ['Pending', 'Overdue'])\
         .order('due_date', desc=False)
        
        # Apply Zone Filter
        if selected_zone:
            query = query.eq('customers.zone_id', selected_zone)
            
        # Apply Search Filter (FIXED LOGIC)
        if search_term:
            # 1. Find matching customers first (Name or Phone)
            cust_query = supabase.table('customers').select('id')\
                .eq('company_id', user['company_id'])\
                .or_(f"full_name.ilike.%{search_term}%,phone_number.ilike.%{search_term}%")\
                .execute()
            
            # Get list of matching Customer IDs
            matching_cust_ids = [c['id'] for c in cust_query.data] if cust_query.data else []
            
            # 2. Build OR Logic: (Invoice Num matches) OR (Customer ID is in the list)
            or_conditions = [f"invoice_number.ilike.%{search_term}%"]
            
            if matching_cust_ids:
                # Add customer matches to the filter if any found
                # Syntax: customer_id.in.(id1,id2,id3)
                ids_str = ",".join(matching_cust_ids)
                or_conditions.append(f"customer_id.in.({ids_str})")
            
            # 3. Apply the combined OR filter
            query = query.or_(",".join(or_conditions))

        inv_res = query.execute()
        if inv_res.data:
            invoices = inv_res.data
            
    except Exception as e:
        print(f"Error fetching unpaid invoices: {e}")
        flash('Could not load invoice data.', 'error')
        
    return render_template('employee_billing.html',  
                           invoices=invoices,  
                           zones=zones,  
                           search_term=search_term,  
                           selected_zone=selected_zone,
                           datetime=datetime)


@app.route('/employee/collect-payment/<invoice_id>', methods=['GET', 'POST'])
@permission_required('can_manage_billing')
def employee_collect_payment(invoice_id):
    user = get_user_from_session()
    
    # --- GET: Show Form ---
    if request.method == 'GET':
        try:
            inv_res = None
            try:
                inv_res = supabase.table('invoices').select('*, customers(full_name)')\
                    .eq('id', invoice_id)\
                    .eq('company_id', user['company_id'])\
                    .in_('status', ['Pending', 'Overdue'])\
                    .maybe_single().execute()
            except APIError as e:
                if "Missing response" not in e.message:
                    raise 
            
            if not inv_res or not inv_res.data:
                flash('Invoice not found or is already paid.', 'error')
                return redirect(url_for('employee_billing'))
                
            invoice = inv_res.data
            return render_template('collect_payment.html', invoice=invoice, datetime=datetime)
    
        except Exception as e:
            print(f"Error fetching invoice for payment: {e}")
            flash(f'Error: {e}', 'error')
            return redirect(url_for('employee_billing'))

    # --- POST: Process Payment & Send Email ---
    if request.method == 'POST':
        try:
            # 1. Fetch Invoice AND Customer Details
            inv_res = supabase.table('invoices').select(
                '*, customers(full_name, email, address, phone_number)'
            ).eq('id', invoice_id).eq('status', 'Pending').maybe_single().execute()
            
            if not inv_res or not inv_res.data:
                flash('This invoice was already paid or does not exist.', 'error')
                return redirect(url_for('employee_billing'))
            
            invoice = inv_res.data
            customer = invoice.get('customers', {}) or {} 

            # 2. Get Form Data
            payment_method = request.form.get('payment_method')
            method_details = request.form.get('method_details')
            transaction_id = request.form.get('transaction_id')

            if not payment_method:
                flash('Please select a payment method.', 'error')
                return redirect(url_for('employee_collect_payment', invoice_id=invoice_id))
            
            if not transaction_id and payment_method != 'Cash':
                flash('Transaction ID is required for this method.', 'error')
                return redirect(url_for('employee_collect_payment', invoice_id=invoice_id))

            payment_method_str = payment_method
            if method_details:
                payment_method_str = f"{payment_method} ({method_details})"

            # 3. Update Database (Mark as Paid)
            paid_at_iso = datetime.now().isoformat()
            
            payload = {
                'status': 'Paid',
                'paid_at': paid_at_iso,
                'payment_method': payment_method_str,
                'transaction_id': transaction_id or "N/A",
                'received_by_employee_id': user['employee_id']  
            }
            
            update_res = supabase.table('invoices').update(payload).eq('id', invoice_id).execute()
            
            if update_res.data:
                # Update local object for receipt generation
                invoice['status'] = 'Paid'
                invoice['paid_at'] = paid_at_iso
                invoice['payment_method'] = payment_method_str
                invoice['transaction_id'] = payload['transaction_id']
                invoice['received_by_employee_id'] = user['employee_id']

                # --- 4. ADMIN NOTIFICATION ---
                try:
                    send_admin_notification(
                        company_id=user['company_id'],
                        title="Payment Received (Manual)",
                        message=f"Employee {user['employee_name']} collected {invoice['amount']} BDT for Invoice #{invoice['invoice_number']} via {payment_method_str}.",
                        notif_type="Payment",
                        related_id=str(invoice['id'])
                    )
                except Exception as e:
                    print(f"Notification Error: {e}")

                # --- 5. BACKGROUND TASKS (SAFE SINGLE THREAD) ---
                def background_payment_tasks(inv_data, cust_data, comp_id, emp_name):
                    print("--- [BACKGROUND] Payment Tasks Started ---")
                    
                    # A. Reactivate Service (Router Connection)
                    try:
                        print(f"--- Reactivating Service for Customer ID: {inv_data['customer_id']} ---")
                        reactivate_service(inv_data['customer_id'])
                        print("--- Reactivation Success ---")
                    except Exception as e:
                        print(f"Reactivation Error: {e}")

                    # B. Send Email
                    try:
                        if cust_data and cust_data.get('email'):
                            print(f"--- Sending Email to {cust_data['email']} ---")
                            company_details = invoice_utils.get_isp_company_details_from_db(comp_id)
                            
                            pdf_gen = invoice_utils.create_thermal_receipt_as_bytes(
                                inv_data, cust_data, company_details, emp_name
                            )
                            
                            if pdf_gen[0]: 
                                pdf_bytes = pdf_gen[1]
                                pdf_filename = f"receipt_{inv_data['invoice_number']}.pdf"
                                
                                # Save temporary file
                                with open(pdf_filename, 'wb') as f:
                                    f.write(pdf_bytes)
                                
                                email_service.send_invoice_email(
                                    customer_email=cust_data['email'],
                                    customer_name=cust_data['full_name'],
                                    invoice_data=inv_data,
                                    company_details=company_details,
                                    pdf_attachment_path=pdf_filename
                                )
                                
                                # Clean up temp file
                                if os.path.exists(pdf_filename):
                                    os.remove(pdf_filename)
                                print("--- Email Sent Successfully ---")
                    except Exception as e:
                        print(f"Email Error: {e}")

                # --- USE EXECUTOR (This prevents the 'Resource temporarily unavailable' error) ---
                executor.submit(
                    background_payment_tasks, 
                    invoice, 
                    customer, 
                    user['company_id'], 
                    user['employee_name']
                )
                # -------------------------------------------------------------------------------

                # C. Audit Log
                log_portal_action("Invoice Paid (Portal)",  
                                f"Collected payment for Invoice #{invoice['invoice_number']} ({invoice['amount']} BDT). "
                                f"Method: {payment_method_str}")
                
                # We return 'email_sent=True' immediately because it is queued safely
                return render_template('payment_success.html', invoice_id=invoice_id, email_sent=True)
                
            else:
                flash('Failed to update invoice status.', 'error')
                
        except Exception as e:
            print(f"Error processing payment: {e}")
            flash(f'An error occurred: {e}', 'error')
            
        return redirect(url_for('employee_collect_payment', invoice_id=invoice_id))


@app.route('/employee/receipt/<invoice_id>/print')
@permission_required('can_manage_billing')
def employee_print_receipt(invoice_id):
    user = get_user_from_session()
    if not supabase: abort(500, "Database connection not available")
    
    try:
        # 1. Fetch Invoice Data
        response = supabase.table('invoices').select(
            '*, customers(full_name, email, address, phone_number)'
        ).eq('id', invoice_id)\
         .eq('company_id', user['company_id'])\
         .eq('status', 'Paid').maybe_single().execute()
        
        if not response.data:
            flash("Receipt not found.", 'error')
            return redirect(url_for('employee_billing'))
            
        invoice_data = response.data
        customer_data = invoice_data.get('customers')
        
        # 2. Generate PDF (In Memory Only)
        company_data = invoice_utils.get_isp_company_details_from_db(user['company_id'])
        
        pdf_gen = invoice_utils.create_thermal_receipt_as_bytes(
            invoice_data,  
            customer_data,  
            company_data,  
            user['employee_name']  
        )
        
        success, pdf_bytes_or_error = pdf_gen
        if not success:
            raise Exception(f"PDF generation failed: {pdf_bytes_or_error}")
            
        pdf_bytes = pdf_bytes_or_error
        
        # 3. Return the File for Printing
        return send_file(
            BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=False,  
            download_name=f"Receipt_{invoice_data['invoice_number']}.pdf"
        )

    except Exception as e:
        print(f"Error generating thermal receipt: {e}")
        flash(f"Error generating receipt: {e}", 'error')
        return redirect(url_for('employee_billing'))

# --- ADD THESE ROUTES TO web_portal/app.py ---

@app.route('/employee/scan-qr')
@employee_login_required
def employee_scan_qr():
    """Shows the QR scanner page."""
    saas_settings = get_saas_settings()
    return render_template('employee_scan.html', 
                           app_name=saas_settings.get('app_name', 'ISP Manager'))

@app.route('/employee/statement', methods=['GET', 'POST'])
@employee_login_required
def employee_statement():
    user = get_user_from_session()
    
    if request.method == 'POST':
        search_term = request.form.get('search_term')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        
        # FIX: Using 'full_name' and 'phone_number' to match your DB schema exactly
        customers = supabase.table('customers').select('*')\
            .or_(f"full_name.ilike.%{search_term}%,email.ilike.%{search_term}%,phone_number.ilike.%{search_term}%")\
            .execute()
        
        found_customers = customers.data or []
        
        # If exactly one customer is found, redirect immediately
        if len(found_customers) == 1:
            customer = found_customers[0]
            return redirect(url_for('view_customer_statement', 
                                    customer_id=customer['id'], 
                                    start=start_date, 
                                    end=end_date))
        
        return render_template('employee_statement_search.html', 
                               customers=found_customers, 
                               search_term=search_term,
                               start_date=start_date,
                               end_date=end_date)

    return render_template('employee_statement_search.html')

@app.route('/employee/statement/view/<customer_id>')
@employee_login_required
def view_customer_statement(customer_id):
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    
    error_message = None
    customer = {}
    invoices = []
    tickets = []
    
    try:
        # 1. Fetch Customer
        cust_res = supabase.table('customers').select('*').eq('id', customer_id).execute()
        customer = cust_res.data[0] if cust_res.data else {}

        # 2. Fetch Invoices
        inv_query = supabase.table('invoices').select('*').eq('customer_id', customer_id)
        if start_date:
            inv_query = inv_query.gte('created_at', start_date)
        if end_date:
            inv_query = inv_query.lte('created_at', f"{end_date}T23:59:59")
        
        invoices = inv_query.order('created_at', desc=True).execute().data or []

        # 3. Fetch Tickets (SAFE MODE: Fetch ALL employee columns)
        # Using 'employees(*)' grabs all columns so we don't crash on name mismatch
        ticket_query = supabase.table('support_tickets').select('*, employees(*)').eq('customer_id', customer_id)
        
        if start_date:
            ticket_query = ticket_query.gte('created_at', start_date)
        if end_date:
            ticket_query = ticket_query.lte('created_at', f"{end_date}T23:59:59")
            
        tickets = ticket_query.order('created_at', desc=True).execute().data or []

    except Exception as e:
        # CAPTURE THE ERROR: This will display the specific DB error on the page
        error_message = str(e)
        print(f"STATEMENT ERROR: {e}") # Also print to console

    # 4. Totals
    total_billed = sum(inv['amount'] for inv in invoices)
    total_paid = sum(inv['amount'] for inv in invoices if inv['status'] == 'Paid')
    total_due = sum(inv['amount'] for inv in invoices if inv['status'] == 'Unpaid')

    return render_template('employee_statement_view.html', 
                           customer=customer, 
                           invoices=invoices, 
                           tickets=tickets,
                           total_billed=total_billed,
                           total_paid=total_paid,
                           total_due=total_due,
                           start_date=start_date,
                           end_date=end_date,
                           error_message=error_message) # Pass error to HTML

@app.route('/employee/inventory/manage/<string:qr_code>', methods=['GET', 'POST'])
@employee_login_required
def employee_manage_item(qr_code):
    """
    Shows item details and handles status updates.
    Includes logic for: Assignments, Replacements, Damages, and Serial Validation.
    """
    user = get_user_from_session()
    saas_settings = get_saas_settings()
    
    # 1. Fetch Item (with Customer Info)
    item = None
    try:
        res = supabase.table('inventory_items').select('*, customers(full_name)').eq('qr_code_id', qr_code).maybe_single().execute()
        
        if not res or not res.data:
            flash("Item not found or invalid QR code.", "error")
            return redirect(url_for('employee_scan_qr'))
        item = res.data
    except Exception as e:
        print(f"Scan Fetch Error: {e}")
        flash("Error fetching item details.", "error")
        return redirect(url_for('employee_scan_qr'))

    # 2. Load Dropdown Data (Zones & Customers)
    customers = []
    zones = []
    try:
        # Fetch Zones
        zone_res = supabase.table('zones').select('id, name').eq('company_id', user['company_id']).execute()
        if zone_res and zone_res.data: 
            zones = zone_res.data
        
        # Fetch Active Customers
        cust_res = supabase.table('customers').select('id, full_name, phone_number, zone_id').eq('company_id', user['company_id']).eq('status', 'Active').order('full_name').execute()
        if cust_res and cust_res.data: 
            customers = cust_res.data
    except Exception as e:
        print(f"Data Load Error: {e}")

    # 3. Handle Form Submission (POST)
    if request.method == 'POST':
        action = request.form.get('action_type')
        
        try:
            # --- ACTION: ASSIGN TO CUSTOMER ---
            if action == 'assign':
                # Security Check: Is it already assigned?
                if item.get('status') == 'Assigned':
                    current_owner = item.get('customers', {}).get('full_name', 'another customer')
                    flash(f"Action Blocked: This item is currently assigned to {current_owner}.", "error")
                    return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))

                cust_id = request.form.get('customer_id')
                new_serial = request.form.get('serial_number')

                # Validation
                if not cust_id:
                    flash("Please select a customer.", "error")
                    return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))
                
                if not new_serial:
                    flash("Please enter the device Serial Number.", "error")
                    return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))
                
                # Unique Serial Check (if changed)
                if new_serial != item['serial_number']:
                    dup_check = supabase.table('inventory_items').select('id').eq('serial_number', new_serial).execute()
                    if dup_check.data and len(dup_check.data) > 0:
                        flash(f"Error: Serial Number '{new_serial}' already exists in inventory!", "error")
                        return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))

                # Execute Assignment
                update_data = { 
                    "status": "Assigned", 
                    "customer_id": cust_id, 
                    "is_configured": True 
                }
                # Update serial if it was PENDING or changed
                if new_serial:
                    update_data['serial_number'] = new_serial
                
                supabase.table('inventory_items').update(update_data).eq('id', item['id']).execute()
                
                log_portal_action("Inventory Assign", f"Employee {user.get('employee_name', 'Tech')} assigned {item['model_name']} (S/N: {new_serial})")
                flash("Item assigned successfully!", "success")
                return redirect(url_for('employee_dashboard'))

            # --- ACTION: REPLACE ITEM ---
            elif action == 'replace':
                reason = request.form.get('notes')
                new_qr = request.form.get('new_qr_id')
                replacement_serial_input = request.form.get('new_serial_number')
                
                # 1. Find New Item by QR
                new_res = supabase.table('inventory_items').select('*').eq('qr_code_id', new_qr).eq('status', 'In Stock').maybe_single().execute()
                if not new_res or not new_res.data:
                    flash("Replacement Failed: The scanned QR code is invalid or the item is not 'In Stock'.", "error")
                    return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))
                
                new_item = new_res.data
                
                # 2. Validate New Item Serial
                final_new_serial = new_item['serial_number']
                if "PENDING" in new_item['serial_number']:
                    if not replacement_serial_input:
                        flash("Replacement Failed: The new item has a PENDING serial. You MUST scan/enter the real Serial Number.", "error")
                        return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))
                    
                    # Check uniqueness for new serial
                    dup_check = supabase.table('inventory_items').select('id').eq('serial_number', replacement_serial_input).execute()
                    if dup_check.data and len(dup_check.data) > 0:
                        flash(f"Error: The new Serial Number '{replacement_serial_input}' is already in use!", "error")
                        return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))
                    
                    final_new_serial = replacement_serial_input
                
                # 3. Mark OLD item as Damaged
                supabase.table('inventory_items').update({
                    "status": "Damaged", 
                    "customer_id": None
                }).eq('id', item['id']).execute()
                
                # 4. Assign NEW item to Customer
                supabase.table('inventory_items').update({
                    "status": "Assigned", 
                    "customer_id": item['customer_id'], 
                    "is_configured": True, 
                    "serial_number": final_new_serial
                }).eq('id', new_item['id']).execute()
                
                log_portal_action("Inventory Replace", f"Replaced faulty {item['serial_number']} with {final_new_serial}. Reason: {reason}")
                flash(f"Replacement successful. New S/N: {final_new_serial}", "success")
                return redirect(url_for('employee_dashboard'))

            # --- ACTION: MARK DAMAGED ---
            elif action == 'damaged':
                notes = request.form.get('notes', '')
                supabase.table('inventory_items').update({"status": "Damaged", "customer_id": None}).eq('id', item['id']).execute()
                log_portal_action("Inventory Damage", f"Marked damaged: {item['model_name']}. Notes: {notes}")
                flash("Item marked as damaged.", "warning")
                return redirect(url_for('employee_dashboard'))

            # --- ACTION: RETURN TO STOCK ---
            elif action == 'stock':
                supabase.table('inventory_items').update({"status": "In Stock", "customer_id": None}).eq('id', item['id']).execute()
                log_portal_action("Inventory Return", f"Returned {item['model_name']} to stock.")
                flash("Item returned to stock.", "info")
                return redirect(url_for('employee_dashboard'))

        except Exception as e:
            print(f"Update Error: {e}")
            flash(f"An unexpected error occurred: {e}", "error")
            return render_template('employee_item_manage.html', item=item, customers=customers, zones=zones, app_name=saas_settings.get('app_name'))

    # 4. Render Page (GET)
    return render_template('employee_item_manage.html', 
                           item=item, 
                           customers=customers,
                           zones=zones, 
                           app_name=saas_settings.get('app_name', 'ISP Manager'))

# --- Helper for Smart ID ---
def generate_smart_id(company_name, index):
    if not company_name: prefix = "EMP"
    else:
        clean = "".join(c for c in company_name if c.isalnum()).upper()
        prefix = clean[:3] if len(clean) >= 3 else clean.ljust(3, 'X')
    return f"{prefix}-{index:03d}"

@app.route('/verify-employee/<string:employee_id>')
def verify_employee(employee_id):
    """
    Public route to verify an employee's identity.
    Calculates Smart ID dynamically.
    """
    saas_settings = get_saas_settings()
    try:
        # 1. Fetch Employee + Company
        res = supabase.table('employees')\
            .select('*, isp_companies(*), employee_roles(role_name)')\
            .eq('id', employee_id)\
            .maybe_single().execute()
        
        employee = res.data
        
        if not employee:
            return render_template('verify_employee.html', error="Invalid ID. Employee not found.", status="invalid")
        
        # 2. Calculate Smart ID (GRE-002)
        smart_id = "Unknown"
        try:
            cid = employee['company_id']
            # Fetch all IDs to find rank
            all_emps = supabase.table('employees').select('id').eq('company_id', cid).order('created_at').execute()
            if all_emps.data:
                ids = [e['id'] for e in all_emps.data]
                if employee_id in ids:
                    rank = ids.index(employee_id) + 1
                    comp_name = employee['isp_companies'].get('company_name', 'EMP')
                    smart_id = generate_smart_id(comp_name, rank)
        except Exception as e:
            print(f"Smart ID Error: {e}")

        # 3. Determine Status
        # The template expects 'active' to show green
        status_code = "active" if employee.get('status') == 'Active' else "inactive"
            
        # 4. Render
        return render_template('verify_employee.html', 
                               employee=employee, 
                               smart_id=smart_id, # <-- Passing the formatted ID
                               company=employee.get('isp_companies', {}),
                               role=employee.get('employee_roles', {}).get('role_name', 'Staff'),
                               status=status_code,
                               app_name=saas_settings.get('app_name', 'ISP Manager'))

    except Exception as e:
        print(f"Verification Error: {e}")
        return render_template('verify_employee.html', error="System Error.", status="error")

def get_company_prefix(name):
    if not name: return "EMP"
    clean = "".join(c for c in name if c.isalnum()).upper()
    return clean[:3] if len(clean) >= 3 else clean.ljust(3, 'X')

@app.route('/verify-manual', methods=['GET', 'POST'])
def verify_manual():
    """Page to manually search for an employee (Phone, UUID, or Smart ID)."""
    saas_settings = get_saas_settings()
    
    if request.method == 'POST':
        term = request.form.get('search_term', '').strip()
        
        if not term:
            flash("Please enter a phone number or ID.", "error")
            return redirect(url_for('verify_manual'))
            
        try:
            # --- 1. SMART ID SEARCH (e.g. GRE-002) ---
            if '-' in term and len(term.split('-')) == 2:
                prefix, num_str = term.split('-')
                prefix = prefix.upper()
                
                if len(prefix) == 3 and num_str.isdigit():
                    target_rank = int(num_str)
                    if target_rank > 0:
                        # A. Find which company matches this prefix
                        # Note: Fetching all companies is fine for low volume. 
                        comps = supabase.table('isp_companies').select('id, company_name').execute().data or []
                        target_cid = None
                        
                        for c in comps:
                            if get_company_prefix(c['company_name']) == prefix:
                                target_cid = c['id']
                                break
                        
                        if target_cid:
                            # B. Find the N-th employee for this company
                            # We use range() (offset) to pick specific rank. 
                            # Rank 1 = Index 0
                            range_start = target_rank - 1
                            res = supabase.table('employees')\
                                .select('id')\
                                .eq('company_id', target_cid)\
                                .order('created_at')\
                                .range(range_start, range_start)\
                                .execute()
                            
                            if res.data and len(res.data) > 0:
                                return redirect(url_for('verify_employee', employee_id=res.data[0]['id']))

            # --- 2. Phone Number Search ---
            # Clean input (remove spaces/dashes if any for phone search)
            clean_term = term.replace(' ', '').replace('-', '')
            if clean_term.isdigit() or (clean_term.startswith('+') and clean_term[1:].isdigit()):
                res = supabase.table('employees').select('id').eq('phone_number', term).maybe_single().execute()
                if res.data:
                    return redirect(url_for('verify_employee', employee_id=res.data['id']))
            
            # --- 3. UUID Search (System ID) ---
            if len(term) > 30: 
                 res = supabase.table('employees').select('id').eq('id', term).maybe_single().execute()
                 if res.data:
                     return redirect(url_for('verify_employee', employee_id=res.data['id']))

            flash(f"No employee found matching '{term}'.", "error")
            
        except Exception as e:
            print(f"Search Error: {e}")
            flash("An error occurred. Please try again.", "error")
            
    return render_template('verify_search.html', 
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           logo_url=saas_settings.get('saas_logo_url'))


# --- SPEED TEST ROUTES (OPTIMIZED) ---

# Pre-generate 50MB of static data in memory at startup (Fast!)
# This prevents CPU bottlenecks during the test.
SPEED_TEST_DATA = b'0' * (50 * 1024 * 1024) 

@app.route('/speed-test')
def speed_test_page():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('speed_test.html')

@app.route('/speed-test/ping')
def speed_test_ping():
    return '', 200

@app.route('/speed-test/download')
def speed_test_download():
    # Return the pre-generated data from RAM
    # Using 'application/octet-stream' prevents the browser from trying to parse it
    return Response(SPEED_TEST_DATA, mimetype='application/octet-stream', headers={"Cache-Control": "no-cache"})

@app.route('/speed-test/upload', methods=['POST'])
def speed_test_upload():
    # Efficiently read and discard upload chunks to test pure bandwidth
    chunk_size = 1024 * 1024 # 1MB chunks
    while True:
        chunk = request.stream.read(chunk_size)
        if not chunk:
            break
    return '', 200
#
# --- *** NEW ORDER TRACKING ROUTES *** ---
#

@app.route('/shop')
def shop():
    """
    Shows the main e-commerce shop page, with a category sidebar,
    search, and sort filters.
    """
    saas_settings = get_saas_settings()
    
    # Get all filter parameters from the URL
    search_term = request.args.get('search', '').strip()
    sort_by = request.args.get('sort_by', 'price_asc') # Default sort
    category_filter_id = request.args.get('category') # The category to filter by
    
    categories = []
    products = []
    
    # --- *** THIS IS THE FIX *** ---
    # We will use two separate try/except blocks.
    # This ensures that if products fail, categories still load.
    
    # 1. Fetch all categories
    try:
        cat_res = supabase.table('product_categories').select('*').order('name').execute()
        categories = cat_res.data or []
    except Exception as e:
        print(f"[DB_ERROR] /shop (Failed to load categories): {e}")
        categories = [] # Set to empty list on error
        
    # 2. Fetch all products
    try:
        query = supabase.rpc('get_products_with_reviews', {
                'p_search_term': search_term or None
            })
            
        # 3. Apply category filter if one is selected
        if category_filter_id:
            query = query.eq('category_id', category_filter_id)
            
        # 4. Apply sorting
        if sort_by == 'price_desc':
            query = query.order('selling_price', desc=True)
        elif sort_by == 'name_asc':
            query = query.order('name', desc=False)
        else: # Default (price_asc)
            query = query.order('selling_price', desc=False)

        # 5. Execute the query
        products_res = query.execute()
        products = products_res.data or []
        
    except Exception as e:
        print(f"[DB_ERROR] /shop (Failed to load products): {e}")
        products = [] # Set to empty list on error
    # --- *** END OF FIX *** ---

    today_date = date.today().isoformat()

    return render_template(
        'shop.html',
        logo_url=saas_settings.get('saas_logo_url'),
        app_name=saas_settings.get('app_name', 'ISP Manager'),
        contact_email=saas_settings.get('contact_email', 'support@huda-it.com'),
        products=products,
        categories=categories, # Pass full list for sidebar
        today_date=today_date,
        search_term=search_term,
        sort_by=sort_by,
        category_filter_id=category_filter_id # Pass current filter
    )

@app.route('/track-order', methods=['GET', 'POST'])
def track_order():
    """
    Page with a form for the user to enter their order number.
    """
    saas_settings = get_saas_settings()
    
    if request.method == 'POST':
        order_number = request.form.get('order_number', '').strip().upper()
        if not order_number:
            flash("Please enter your order number.", "error")
            return redirect(url_for('track_order'))
        
        # Check if order exists
        try:
            order_res = supabase.table('saas_orders').select('id').eq('order_number', order_number).maybe_single().execute()
            
            # --- *** THIS IS THE FIX *** ---
            # 1. First, check if the response object itself is valid
            # 2. Then, check if data was found
            if not order_res or not order_res.data:
                flash("Order not found. Please check your order number and try again.", "error")
                return redirect(url_for('track_order'))
            # --- *** END OF FIX *** ---
            
            # Order found, redirect to the status page
            return redirect(url_for('order_status', order_number=order_number))
            
        except Exception as e:
            print(f"Error checking order status: {e}")
            flash(f"An error occurred: {e}", "error")
            return redirect(url_for('track_order'))

    # --- GET Request ---
    return render_template('track_order.html',
                           developer_logo=saas_settings.get('saas_logo_url'),
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           contact_email=saas_settings.get('contact_email', 'support@huda-it.com'))


@app.route('/order-status/<string:order_number>')
def order_status(order_number):
    """
    Displays the status of a specific order.
    """
    saas_settings = get_saas_settings()
    order_data = None
    
    try:
        order_res = supabase.table('saas_orders').select('*').eq('order_number', order_number.upper()).maybe_single().execute()
        if not order_res.data:
            flash("Order not found.", "error")
            return redirect(url_for('track_order'))
        
        order_data = order_res.data
        
    except Exception as e:
        print(f"Error fetching order status: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('track_order'))

    return render_template('order_status.html',
                           order=order_data,
                           developer_logo=saas_settings.get('saas_logo_url'),
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           contact_email=saas_settings.get('contact_email', 'support@huda-it.com'),
                           app_download_url=saas_settings.get('app_download_url')) # Pass the download link

# --- REPLACE this function in app.py ---

@app.route('/product-checkout', methods=['GET', 'POST'])
def product_checkout():
    """
    Checkout page logic with FIXED Variable Scope & Email Arguments.
    """
    saas_settings = get_saas_settings()
    cart = session.get('cart', {})
    user = session.get('user', {})
    
    # 1. Validation
    if not cart:
        flash("Your cart is empty.", "info")
        return redirect(url_for('cart'))

    # 2. Setup Data
    shipping_cost = float(saas_settings.get('shipping_cost', 0.0))
    subtotal = 0
    cart_products_snapshot = []
    product_ids = list(cart.keys())
    fetched_products = []
    
    # 3. Data Fetching
    try:
        # Try RPC first (Bypass RLS)
        res = supabase.rpc('get_products_with_reviews', {'p_search_term': ""}).execute()
        if res.data:
            fetched_products = [p for p in res.data if str(p['id']) in product_ids]
            
        # Fallback to Table
        if not fetched_products:
             res_table = supabase.table('products').select('*, category_id').in_('id', product_ids).execute()
             if res_table.data:
                 fetched_products = res_table.data
    except Exception as e:
        print(f"Checkout Data Error: {e}")

    if not fetched_products:
        flash("Items unavailable.", "error")
        return redirect(url_for('cart'))

    # 4. Calculations
    today_date = date.today().isoformat()
    for product in fetched_products:
        pid = str(product['id'])
        if pid not in cart: continue
        qty = cart[pid]
        
        # Stock Check
        if product.get('stock_quantity', 0) < qty:
            flash(f"Not enough stock for {product.get('name')}.", "error")
            return redirect(url_for('cart'))

        op = float(product.get('selling_price', 0))
        fp = op
        
        # --- FIX: Initialize variable BEFORE the if block ---
        is_discounted = False 
        
        start = product.get('discount_start_date') or '1970-01-01'
        end = product.get('discount_end_date') or '2099-12-31'
        
        if product.get('discount_percent', 0) > 0 and start <= today_date and end >= today_date:
            fp = op * (1 - (float(product.get('discount_percent', 0)) / 100))
            is_discounted = True
            
        item_sub = fp * qty
        subtotal += item_sub
        
        cart_products_snapshot.append({
            "id": pid,
            "name": product.get('name'),
            "image_url": product.get('image_url'),
            "quantity": qty,
            "subtotal": item_sub,
            "original_price": op,
            "final_price_per_item": fp,
            "is_discounted": is_discounted, # Now safely defined
            "category_id": product.get('category_id')
        })
    
    # 5. Promo Logic
    discount_amount = 0.0
    promo = session.get('promo')
    promo_code_used = None
    if promo:
        promo_code_used = promo['code']
        if promo['type'] == 'Percentage':
            discount_amount = subtotal * (promo['value'] / 100)
        else:
            discount_amount = promo['value']
        if discount_amount > subtotal: discount_amount = subtotal

    total_price = max(0, subtotal + shipping_cost - discount_amount)

    # --- 6. HANDLE ORDER SUBMISSION ---
    if request.method == 'POST':
        payment_choice = request.form.get('payment_method')
        street = request.form.get('address')
        full_address = f"{street}, {request.form.get('thana')}, {request.form.get('district')}, {request.form.get('division')}"
        
        form_data = {
            "full_name": request.form.get('full_name'),
            "email": request.form.get('email').strip().lower(),
            "phone": request.form.get('phone'),
            "address": full_address
        }
        
        if not all([form_data['full_name'], form_data['email'], form_data['phone'], street]):
            flash("All fields are required.", "error")
            return redirect(url_for('product_checkout'))
            
        try:
            # Generate Order Number
            order_num_res = supabase.rpc('generate_new_product_order_number').execute()
            new_order_number = order_num_res.data or f"ORD-{int(datetime.now().timestamp())}"

            order_payload = {
                "order_number": new_order_number, "customer_details": form_data, 
                "order_items": cart_products_snapshot, "shipping_cost": shipping_cost,
                "discount_amount": discount_amount, "promo_code": promo_code_used,
                "total_amount": total_price, "status": "Pending Payment", "payment_method": "Unknown"
            }

            # A) COD Payment
            if payment_choice == 'cod':
                order_payload['status'] = 'Processing (COD)'
                order_payload['payment_method'] = 'Cash on Delivery'
                supabase.table('product_orders').insert(order_payload).execute()
                
                # --- EMAIL LOGIC ---
                try:
                    # PASS ALL ARGUMENTS INCLUDING DISCOUNT_AMOUNT
                    email_service.send_product_order_confirmation_customer(
                        saas_settings, 
                        form_data['email'], 
                        form_data, # Full details for address
                        new_order_number, 
                        cart_products_snapshot, 
                        total_price,
                        shipping_cost, 
                        discount_amount, # <--- THIS WAS MISSING
                        payment_details=None
                    )
                except Exception as e: 
                    print(f"Customer Email Error: {e}")
                
                try:
                    # Robust Admin Email Fallback
                    admin_email = (saas_settings.get('contact_email') or 
                                   saas_settings.get('brevo_sender_email') or 
                                   saas_settings.get('smtp_user') or
                                   os.environ.get('SENDER_EMAIL'))
                    
                    if admin_email:
                        try:
                            admin_body = render_template('product_order_admin_email.html',
                                form_data=form_data, order_items=cart_products_snapshot,
                                total_amount=total_price, shipping_cost=shipping_cost,
                                order_number=new_order_number, payment_details=None)
                        except:
                            admin_body = f"New COD Order #{new_order_number}. Total: {total_price}."
                        
                        email_service.send_generic_email(saas_settings, admin_email, f"New Order: {new_order_number}", admin_body)
                except Exception as e: print(f"Admin Email Error: {e}")

                session.pop('cart', None); session.pop('promo', None)
                flash("Order placed successfully!", "success")
                return redirect(url_for('product_order_success', order_number=new_order_number))

            # B) Online Payment
            elif payment_choice == 'pay_now':
                if not saas_settings.get('gateway_enabled', False):
                    flash("Online payments disabled.", "error"); return redirect(url_for('product_checkout'))
                
                shurjopay = initialize_shurjopay(saas_settings, return_url=url_for('product_payment_return', _external=True), cancel_url=url_for('product_payment_cancel', _external=True))
                pp_obj = SimpleNamespace(amount=total_price, order_id=new_order_number, 
                    customer_name=form_data['full_name'], customer_phone=form_data['phone'],
                    customer_email=form_data['email'], customer_city="Dhaka", 
                    customer_address=form_data['address'], currency="BDT", customer_post_code="1200")
                response = shurjopay.make_payment(pp_obj)
                
                if hasattr(response, 'checkout_url'):
                    order_payload['checkout_url'] = response.checkout_url
                    order_payload['gateway_tx_id'] = response.sp_order_id
                    res = supabase.table('product_orders').insert(order_payload).execute()
                    if res.data: session['pending_order_id'] = res.data[0]['id']
                    return redirect(response.checkout_url)
                
                flash("Gateway error.", "error"); return redirect(url_for('product_checkout'))
            
            else:
                flash("Invalid payment method.", "error"); return redirect(url_for('product_checkout'))

        except Exception as e:
            print(f"Order Error: {e}"); flash(f"Order failed: {e}", "error")
            return redirect(url_for('product_checkout'))

    # --- 7. RENDER ---
    prefill = {'name': '', 'email': '', 'phone': '', 'address': ''}
    if user:
        prefill['name'] = user.get('customer_name') or user.get('employee_name') or ''
        prefill['email'] = user.get('email') or ''
        if user.get('customer_id'):
            try:
                cust = supabase.table('customers').select('phone_number, address').eq('id', user['customer_id']).maybe_single().execute()
                if cust.data: prefill['phone'] = cust.data.get('phone_number', ''); prefill['address'] = cust.data.get('address', '')
            except: pass

    return render_template('product_checkout.html', logo_url=saas_settings.get('saas_logo_url'),
        app_name=saas_settings.get('app_name'), contact_email=saas_settings.get('contact_email'),
        cart_products=cart_products_snapshot, subtotal=subtotal, shipping_cost=shipping_cost,
        discount_amount=discount_amount, total_price=total_price, prefill=prefill)

@app.route('/product-order-success/<string:order_number>')
def product_order_success(order_number):
    """
    Shows the 'Thank You' page after a successful product order.
    """
    saas_settings = get_saas_settings()
    order_data = None
    
    try:
        order_res = supabase.table('product_orders').select('*').eq('order_number', order_number.upper()).maybe_single().execute()
        if not order_res.data:
            flash("Order not found.", "error")
            return redirect(url_for('product_track'))
        
        order_data = order_res.data
        
    except Exception as e:
        print(f"Error fetching product order status: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('product_track'))

    return render_template('product_order_success.html',
                           order=order_data,
                           # --- THIS IS THE FIX ---
                           developer_logo=saas_settings.get('saas_logo_url'), # Use developer_logo
                           logo_url=saas_settings.get('saas_logo_url'), # or logo_url
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           contact_email=saas_settings.get('contact_email', 'support@huda-it.com')
                           # --- END OF FIX ---
                           )

@app.route('/product-payment-return', methods=['GET'])
def product_payment_return():
    """
    Callback URL for ShurjoPay after a PRODUCT payment.
    """
    order_id_from_sp = request.args.get('order_id')
    pending_order_id = session.pop('pending_order_id', None)

    if not order_id_from_sp or order_id_from_sp.startswith("NOK"):
        flash("Payment was cancelled or failed. Please try again.", "error")
        # Cleanup pending order
        try:
            if pending_order_id:
                supabase.table('product_orders').delete().eq('id', pending_order_id).execute()
            elif order_id_from_sp:
                supabase.table('product_orders').delete().eq('gateway_tx_id', order_id_from_sp).execute()
        except: pass
        return redirect(url_for('cart'))

    saas_settings = get_saas_settings()
    is_sandbox = saas_settings.get('gateway_sandbox_enabled', True)
    
    try:
        response = None
        if is_sandbox:
            response = {"sp_code": 1000, "message": "Sandboxed Payment Success", "order_id": order_id_from_sp, "method": "Sandbox Test Card", "bank_trx_id": "MOCK", "currency": "BDT"}
        else:
            response = safe_verify_payment(saas_settings, order_id_from_sp)
            
        if isinstance(response, dict) and response.get('sp_code') == 1000:
            # Payment Verified
            gateway_tx_id = response.get('order_id') 
            order_res = supabase.table('product_orders').select('*').eq('gateway_tx_id', gateway_tx_id).maybe_single().execute()
            
            if not order_res.data: raise Exception("Order missing")
            order = order_res.data
            
            supabase.table('product_orders').update({
                "status": "Processing (Paid)", 
                "payment_method": response.get('method'),
                "transaction_id": response.get('bank_trx_id')
            }).eq('id', order['id']).execute()

            # --- EMAIL LOGIC (UPDATED FOR NEW SIGNATURE) ---
            form_data = order.get('customer_details', {})
            order_items = order.get('order_items', [])
            total_amount = float(order.get('total_amount', 0))
            shipping_cost = float(order.get('shipping_cost', 0.0))
            discount_amount = float(order.get('discount_amount', 0.0)) # <--- Get discount
            
            try:
                response['amount'] = total_amount 
                
                email_service.send_product_order_confirmation_customer(
                    saas_settings, 
                    to_email=form_data.get('email'), 
                    customer_details=form_data, # <--- Pass FULL dict, not name
                    order_number=order['order_number'], 
                    order_items=order_items,
                    total_amount=total_amount,
                    shipping_cost=shipping_cost,
                    discount_amount=discount_amount, # <--- Pass discount
                    payment_details=response
                )
            except Exception as e: print(f"Customer Email Error: {e}")
            
            # Send Admin Email
            try:
                admin_email = (saas_settings.get('contact_email') or saas_settings.get('brevo_sender_email') or os.environ.get('SENDER_EMAIL'))
                if admin_email:
                    try:
                        admin_body = render_template('product_order_admin_email.html',
                                                      form_data=form_data, order_items=order_items,
                                                      total_amount=total_amount, shipping_cost=shipping_cost,
                                                      order_number=order['order_number'], payment_details=response)
                    except: admin_body = f"New Paid Order #{order['order_number']}"
                    
                    email_service.send_generic_email(saas_settings, admin_email, f"New PAID Product Order: {order['order_number']}", admin_body)
            except Exception as e: print(f"Admin Email Error: {e}")
            
            session.pop('cart', None)
            flash("Payment successful! Your order is being processed.", "success")
            return redirect(url_for('product_order_success', order_number=order['order_number']))
            
        else:
            # Payment Failed
            flash(f"Payment Failed: {response.get('message')}", "error")
            if pending_order_id: supabase.table('product_orders').delete().eq('id', pending_order_id).execute()
            return redirect(url_for('product_checkout'))

    except Exception as e:
        print(f"Callback Error: {e}")
        flash("An error occurred.", "error")
        return redirect(url_for('product_checkout'))


@app.route('/product-payment-cancel', methods=['GET', 'POST'])
def product_payment_cancel():
    """
    Callback URL if the user cancels the product payment.
    """
    flash("Your payment was cancelled. You can try again or choose 'Cash on Delivery'.", "info")
    
    # --- *** THIS IS THE FIX *** ---
    # Get the pending order ID from the session and delete the order
    pending_order_id = session.pop('pending_order_id', None)
    if pending_order_id:
        try:
            supabase.table('product_orders').delete().eq('id', pending_order_id).execute()
            print(f"Deleted cancelled order {pending_order_id}.")
        except Exception as e:
            print(f"Error deleting cancelled order {pending_order_id}: {e}")
    # --- *** END OF FIX *** ---
    
    return redirect(url_for('product_checkout'))

# --- *** END OF NEW ROUTES *** ---

@app.route('/product-track', methods=['GET', 'POST'])
def product_track():
    """
    Page with a form for the user to enter their PRODUCT order number.
    """
    saas_settings = get_saas_settings()
    
    if request.method == 'POST':
        order_number = request.form.get('order_number', '').strip().upper()
        if not order_number:
            flash("Please enter your order number.", "error")
            return redirect(url_for('product_track'))
        
        # Check if order exists
        try:
            order_res = supabase.table('product_orders').select('id').eq('order_number', order_number).maybe_single().execute()
            
            if not order_res or not order_res.data:
                flash("Order not found. Please check your order number and try again.", "error")
                return redirect(url_for('product_track'))
            
            # Order found, redirect to the status page
            return redirect(url_for('product_order_status', order_number=order_number))
            
        except Exception as e:
            print(f"Error checking product order status: {e}")
            flash(f"An error occurred: {e}", "error")
            return redirect(url_for('product_track'))

    # --- GET Request ---
    return render_template('track_product_order.html',
                           developer_logo=saas_settings.get('saas_logo_url'),
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           contact_email=saas_settings.get('contact_email', 'support@huda-it.com'))

# ============================================================
#  PASTE THE NEW HELPER FUNCTIONS HERE
# ============================================================

def _get_pathao_access_token(settings):
    """Helper to get Pathao token for the web portal."""
    try:
        base_url = "https://courier-api-sandbox.pathao.com" # Sandbox
        # base_url = "https://api-hermes.pathao.com" # Live
        
        payload = {
            "client_id": settings.get('courier_pathao_client_id', '').strip(),
            "client_secret": settings.get('courier_pathao_client_secret', '').strip(),
            "username": settings.get('courier_pathao_username', '').strip(),
            "password": settings.get('courier_pathao_password', '').strip(),
            "grant_type": "password"
        }
        res = requests.post(f"{base_url}/aladdin/api/v1/issue-token", json=payload)
        return res.json().get('access_token')
    except:
        return None

def fetch_live_courier_status(order, settings):
    """Fetches the real-time status from Steadfast or Pathao."""
    courier = order.get('courier_name')
    cid = order.get('courier_consignment_id')
    
    if not courier or not cid:
        return None

    try:
        if courier == 'Steadfast':
            url = f"https://portal.steadfast.com.bd/api/v1/status_by_cid/{cid}"
            headers = {
                "Api-Key": settings.get('courier_steadfast_api_key', '').strip(),
                "Secret-Key": settings.get('courier_steadfast_secret_key', '').strip()
            }
            res = requests.get(url, headers=headers)
            data = res.json()
            if data.get('status') == 200:
                return data.get('delivery_status', 'Unknown').capitalize()
                
        elif courier == 'Pathao':
            token = _get_pathao_access_token(settings)
            if token:
                base_url = "https://courier-api-sandbox.pathao.com" # Sandbox
                # base_url = "https://api-hermes.pathao.com" # Live
                
                url = f"{base_url}/aladdin/api/v1/orders/{cid}"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
                res = requests.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json().get('data', {})
                    status = data.get('order_status', 'Unknown')
                    return status.replace('_', ' ').capitalize()
                    
    except Exception as e:
        print(f"Error fetching courier status: {e}")
        
    return None

# ============================================================
#  END OF NEW HELPER FUNCTIONS
# ============================================================


@app.route('/product-order-status/<string:order_number>')
def product_order_status(order_number):
    """
    Displays the status of a specific e-commerce product order.
    """
    saas_settings = get_saas_settings()
    order_data = None
    subtotal = 0.0 
    discount_amount = 0.0
    live_courier_status = None
    
    try:
        order_res = supabase.table('product_orders').select('*').eq('order_number', order_number.upper()).maybe_single().execute()
        if not order_res.data:
            flash("Order not found.", "error")
            return redirect(url_for('product_track'))
        
        order_data = order_res.data
        
        # --- 1. Calculate Subtotal ---
        order_items = order_data.get('order_items', [])
        for item in order_items:
            # We use final_price_per_item to show the "Shelf Price" sum
            price = float(item.get('final_price_per_item', item.get('price_per_item', 0)))
            qty = int(item.get('quantity', 1))
            subtotal += (price * qty)
            
        # --- 2. Get Promo Discount ---
        discount_amount = float(order_data.get('discount_amount', 0.0))
        
        # --- 3. Fetch Live Courier Status ---
        if order_data.get('courier_name') and order_data.get('courier_consignment_id'):
            live_courier_status = fetch_live_courier_status(order_data, saas_settings)
        
    except Exception as e:
        print(f"Error fetching product order status: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('product_track'))

    return render_template('product_order_status.html',
                           order=order_data,
                           subtotal=subtotal,
                           discount_amount=discount_amount, # <-- Pass discount
                           live_courier_status=live_courier_status,
                           developer_logo=saas_settings.get('saas_logo_url'),
                           app_name=saas_settings.get('app_name', 'ISP Manager'),
                           contact_email=saas_settings.get('contact_email', 'support@huda-it.com'))

@app.route('/submit-review/<uuid:product_id>', methods=['POST'])
def submit_review(product_id):
    """
    Handles the submission of a new product review.
    """
    try:
        # 1. Get form data
        form_data = {
            "rating": int(request.form.get('rating', 5)),
            "reviewer_name": request.form.get('reviewer_name'),
            "reviewer_email": request.form.get('reviewer_email').strip().lower(),
            "review_title": request.form.get('review_title'),
            "review_body": request.form.get('review_body')
        }
        product_id_str = str(product_id)

        if not all([form_data['reviewer_name'], form_data['reviewer_email'], form_data['review_title'], form_data['review_body']]):
            flash("Please fill out all fields to submit your review.", "error")
            return redirect(url_for('product_detail', product_id=product_id))

        # 2. *** SECURITY CHECK ***
        # Check if this email has purchased this product and had it delivered.
        
        # --- *** THIS IS THE FIX *** ---
        # We must convert the Python dictionary to a JSON string for the .contains() filter
        contains_payload = json.dumps([{"id": product_id_str}])
        
        query = supabase.table('product_orders')\
            .select('id')\
            .eq('customer_details->>email', form_data['reviewer_email'])\
            .eq('status', 'Delivered')\
            .contains('order_items', contains_payload)\
            .limit(1)\
            .execute()
        # --- *** END OF FIX *** ---
        
        if not query.data:
            flash("Error: You can only review products you have purchased and received.", "error")
            return redirect(url_for('product_detail', product_id=product_id))
            
        # 3. All checks passed. Save the review.
        review_payload = {
            "product_id": product_id_str,
            "rating": form_data['rating'],
            "reviewer_name": form_data['reviewer_name'],
            "reviewer_email": form_data['reviewer_email'],
            "review_title": form_data['review_title'],
            "review_body": form_data['review_body'],
            "status": "Pending Approval" # Admin must approve it
        }
        
        supabase.table('product_reviews').insert(review_payload).execute()
        
        flash("Thank you! Your review has been submitted for approval.", "success")
        return redirect(url_for('product_detail', product_id=product_id))

    except Exception as e:
        print(f"CRITICAL: Failed to submit review: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('product_detail', product_id=product_id))

@app.route('/sw.js')
def service_worker():
    """
    Serves the Service Worker from the root path to allow PWA installation.
    """
    response = send_from_directory('static', 'sw.js')
    # This header ensures the browser always checks for updates to the SW
    response.headers['Cache-Control'] = 'no-cache'
    return response

# --- ADD THIS NEAR YOUR OTHER ROUTES ---
@app.route('/health')
def health_check():
    """Lightweight route to keep the server awake."""
    return "OK", 200

@app.route('/logout')
def logout():
    session.pop('user', None); print("User logged out.")
    try:
        if supabase and supabase.auth.get_session(): supabase.auth.sign_out(); print("Supabase session signed out.")
    except Exception as e: print(f"Error signing out from Supabase: {e}")
    flash('Logged out successfully.', 'info'); return redirect(url_for('login'))

# --- ADD THIS CODE BLOCK AFTER creating 'app = Flask(__name__)' ---

@app.before_request
def track_visitor():
    """Logs visitor activity. Ignores static files and favicons."""
    # 1. Filter out static files, internal routes, and FAVICONS (Fixes doubling)
    if (request.path.startswith('/static') or 
        request.path.startswith('/_') or 
        'favicon' in request.path):
        return
        
    try:
        user_id = session.get('user', {}).get('id') if 'user' in session else None
        
        # 2. Log IP AND User Agent (Fixes Phone+Laptop showing as 1)
        supabase.table('portal_analytics').insert({
            'ip_address': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', 'Unknown'), # <--- NEW
            'path': request.path,
            'user_id': str(user_id) if user_id else None
        }).execute()
    except Exception:
        pass


if __name__ == '__main__':

    app.run(port=5000)










