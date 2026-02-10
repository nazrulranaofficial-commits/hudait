import requests
import logging
import json
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ShurjoPayConfigModel:
    """
    Configuration model for ShurjoPay.
    """
    def __init__(self, username, password, prefix, return_url, cancel_url, api_url=None):
        self.username = username
        self.password = password
        self.prefix = prefix
        self.return_url = return_url
        self.cancel_url = cancel_url
        # Default to sandbox if no URL provided, change to live URL for production
        self.api_url = api_url or "https://sandbox.shurjopayment.com" 

class ShurjopayPlugin:
    """
    Main class to handle ShurjoPay interactions.
    """
    def __init__(self, config: ShurjoPayConfigModel):
        self.config = config
        self.token = None
        self.store_id = None
        
    def get_token(self):
        """
        Authenticates with ShurjoPay and retrieves a transaction token.
        """
        try:
            url = f"{self.config.api_url}/api/get_token"
            payload = {
                "username": self.config.username,
                "password": self.config.password
            }
            headers = {'Content-Type': 'application/json'}
            
            response = requests.post(url, json=payload, headers=headers)
            data = response.json()
            
            if response.status_code == 200 and 'checkout_url' in data:
                # Note: Some versions return the token differently. 
                # Usually it sets a token for subsequent requests.
                # For standard ShurjoPay, the 'get_token' endpoint returns the token 
                # which acts as the authorization bearer.
                self.token = data.get('token')
                self.store_id = data.get('store_id')
                return self.token
            else:
                logger.error(f"ShurjoPay Token Error: {data}")
                return None
        except Exception as e:
            logger.error(f"ShurjoPay Connection Error: {e}")
            return None

    def make_payment(self, payment_request):
        """
        Initiates a payment request.
        
        Args:
            payment_request (SimpleNamespace or dict): Contains amount, order_id, customer details.
        
        Returns:
            response object with 'checkout_url' and 'sp_order_id' attributes/keys.
        """
        # Ensure we have a token
        if not self.token:
            if not self.get_token():
                raise Exception("Failed to authenticate with ShurjoPay")

        url = f"{self.config.api_url}/api/secret-pay"
        
        # Determine client IP (placeholder if not available)
        client_ip = getattr(payment_request, 'client_ip', '127.0.0.1')
        
        payload = {
            "prefix": self.config.prefix,
            "token": self.token,
            "return_url": self.config.return_url,
            "cancel_url": self.config.cancel_url,
            "store_id": self.store_id,
            "amount": payment_request.amount,
            "order_id": payment_request.order_id,
            "currency": getattr(payment_request, 'currency', 'BDT'),
            "customer_name": payment_request.customer_name,
            "customer_address": payment_request.customer_address,
            "customer_email": payment_request.customer_email,
            "customer_phone": payment_request.customer_phone,
            "customer_city": getattr(payment_request, 'customer_city', 'Dhaka'),
            "client_ip": client_ip
        }
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}'
        }

        try:
            response = requests.post(url, json=payload, headers=headers)
            data = response.json()
            
            # Create a simple object to return consistent results
            class ShurjoResponse:
                def __init__(self, checkout_url, sp_order_id, message=None):
                    self.checkout_url = checkout_url
                    self.sp_order_id = sp_order_id
                    self.message = message

            if 'checkout_url' in data:
                return ShurjoResponse(data['checkout_url'], data.get('sp_order_id'))
            else:
                logger.error(f"Payment Initiation Failed: {data}")
                return ShurjoResponse(None, None, message="Payment initiation failed")
                
        except Exception as e:
            logger.error(f"Payment Request Exception: {e}")
            raise e

    def verify_payment(self, order_id):
        """
        Verifies a payment status using the Order ID.
        """
        if not self.token:
            self.get_token()
            
        url = f"{self.config.api_url}/api/verification"
        payload = {
            "order_id": order_id,
            "token": self.token
        }
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}'
        }
        
        try:
            response = requests.post(url, json=payload, headers=headers)
            data = response.json()
            # Returns the raw list of transaction objects/dicts
            return data 
        except Exception as e:
            logger.error(f"Verification Error: {e}")
            return []