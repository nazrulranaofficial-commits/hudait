import datetime
import threading
from database import supabase
import routeros_api
import email_service

class PortalRouterManager:
    def __init__(self, ip, user, password, port=8728):
        self.ip = ip
        self.user = user
        self.password = password
        self.port = int(port)

    def connect(self):
        if not self.ip: return None
        try:
            connection = routeros_api.RouterOsApiPool(
                self.ip,
                username=self.user,
                password=self.password,
                port=self.port,
                plaintext_login=True
            )
            return connection
        except Exception as e:
            print(f"Router Connection Error: {e}")
            return None

    def enable_internet(self, username):
        """Enables a PPPoE user on the router."""
        connection = self.connect()
        if not connection: return False
        
        try:
            api = connection.get_api()
            secrets = api.get_resource('/ppp/secret')
            user = secrets.get(name=username)
            
            if user:
                secrets.set(id=user[0]['id'], disabled='no')
                print(f"✅ Router: User {username} enabled successfully.")
                return True
            else:
                print(f"⚠️ Router: User {username} not found.")
                return False
        except Exception as e:
            print(f"❌ Router Error: {e}")
            return False
        finally:
            connection.disconnect()

def reactivate_service(customer_id):
    print(f"--- Reactivating Service for Customer ID: {customer_id} ---")
    
    next_due = datetime.date.today() + datetime.timedelta(days=30)
    
    try:
        # 1. Fetch CURRENT Info First
        fetch_res = supabase.table('customers').select('status, email, full_name, pppoe_username, company_id').eq('id', customer_id).maybe_single().execute()
        
        if not fetch_res.data:
            return False, "Customer not found."
            
        customer = fetch_res.data
        previous_status = customer.get('status')
        username = customer.get('pppoe_username')
        company_id = customer.get('company_id')
        
        # 2. Update Database
        supabase.table('customers').update({
            'status': 'Active',
            'next_payment_date': next_due.isoformat()
        }).eq('id', customer_id).execute()

        # 3. Check Condition: Was Suspended? -> Send Email
        if previous_status == 'Suspended':
            print(f"🔄 Customer was suspended. Sending Reactivation Email...")
            if customer.get('email'):
                try:
                    # --- FIX: Passing company_id here ---
                    email_service.send_service_reactivated_email(customer['email'], customer['full_name'], company_id)
                except Exception as e:
                    print(f"⚠️ Failed to send reactivation email: {e}")

        # 4. Router Activation
        if not username:
            return True, "Database updated, but no PPPoE username found."

        # Fetch Router Settings
        company_res = supabase.table('isp_companies').select(
            'router_ip, router_user, router_password, router_api_port'
        ).eq('id', company_id).maybe_single().execute()

        if not company_res.data:
            return True, "Database updated, but Company Router settings not found."

        settings = company_res.data
        
        def router_task():
            router = PortalRouterManager(
                ip=settings.get('router_ip'),
                user=settings.get('router_user'),
                password=settings.get('router_password'),
                port=settings.get('router_api_port', 8728)
            )
            router.enable_internet(username)

        threading.Thread(target=router_task, daemon=True).start()
        
        return True, f"Service reactivated for {username}."
            
    except Exception as e:
        print(f"Reactivation Error: {e}")
        return False, str(e)