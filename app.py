import os
import streamlit as st
from datetime import datetime, timedelta
import pandas as pd
import xmlrpc.client
import re
from collections import defaultdict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# === Config ===
CONFIG = {
    'url': os.getenv('ODOO_URL'),
    'db': os.getenv('ODOO_DB'),
    'username': os.getenv('ODOO_USERNAME'),
    'password': os.getenv('ODOO_PASSWORD'),
    'hq_company_name': os.getenv('HQ_COMPANY_NAME'),
    'app_username': os.getenv('APP_USERNAME'),
    'app_password': os.getenv('APP_PASSWORD'),
}

# === Vendor Names to Exclude ===
EXCLUDED_PARTNER_NAMES = [
    "Wedtree eStore Private Limited - HO",
    "Wedtree eStore Private Limited - Coimbatore",
    "Wedtree eStore Private Limited - T Nagar",
    "Wedtree eStore Private Limited - Online",
    "Wedtree eStore Private Limited - Vizag",
    "Saree Trails",
    "Wedtree eStore Private Limited - Malleshwaram",
    "Wedtree eStore Private Limited - Jayanagar",
    "Wedtree eStore Private Limited - Hyderabad"
]

# === Helper Functions ===
def extract_sku_from_product_name(product_name):
    if not product_name:
        return "N/A"
    match = re.search(r'([A-Za-z0-9\-]+)$', product_name.strip())
    return match.group(1) if match else "N/A"

def connect_odoo():
    try:
        common = xmlrpc.client.ServerProxy(CONFIG['url'] + 'xmlrpc/2/common')
        uid = common.authenticate(CONFIG['db'], CONFIG['username'], CONFIG['password'], {})
        models = xmlrpc.client.ServerProxy(CONFIG['url'] + 'xmlrpc/2/object')
        return uid, models
    except Exception as e:
        st.error(f"Failed to connect to Odoo: {str(e)}")
        return None, None

def get_hq_company_id(models):
    try:
        hq_company_id = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'res.company', 'search',
            [[['name', '=', CONFIG['hq_company_name']]]])[0]
        return hq_company_id
    except:
        st.error(f"Failed to find company: {CONFIG['hq_company_name']}")
        return None

def lookup_lot_numbers(lot_numbers, models, hq_company_id):
    try:
        move_lines = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'stock.move.line', 'search_read',
            [[
                ['lot_name', 'in', lot_numbers],
                ['company_id', '=', hq_company_id]
            ]],
            {'fields': ['lot_name', 'picking_id', 'product_id']})

        if not move_lines:
            st.warning("No stock move lines found for the given lot numbers.")
            return None

        # Map Picking IDs and Product IDs
        picking_ids = list(set(ml['picking_id'][0] for ml in move_lines if ml['picking_id']))
        product_ids = list(set(ml['product_id'][0] for ml in move_lines if ml['product_id']))

        # Fetch Picking Details
        pickings = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'stock.picking', 'read',
            [picking_ids],
            {'fields': ['id', 'name', 'origin', 'partner_id']})
        picking_map = {p['id']: p for p in pickings}

        # Filter Pickings (Remove excluded vendors)
        filtered_picking_ids = [
            p['id'] for p in pickings
            if p['partner_id'] and p['partner_id'][1] not in EXCLUDED_PARTNER_NAMES
        ]

        # Fetch Product Names
        products = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'product.product', 'read',
            [product_ids],
            {'fields': ['id', 'name']})
        product_map = {p['id']: p['name'] for p in products}

        # Grouping data
        grouped_data = defaultdict(lambda: {'lots': set(), 'unit_price': 0.0, 'discount': 0.0})

        for ml in move_lines:
            picking_id = ml['picking_id'][0] if ml['picking_id'] else None
            product_id = ml['product_id'][0] if ml['product_id'] else None

            if not picking_id or picking_id not in filtered_picking_ids:
                continue

            picking = picking_map[picking_id]
            product_name = product_map.get(product_id, 'N/A')
            sku = extract_sku_from_product_name(product_name)
            po_name = picking['origin']
            vendor_name = picking['partner_id'][1] if picking['partner_id'] else "Unknown Vendor"

            # Get Purchase Order ID
            po_ids = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
                'purchase.order', 'search',
                [[['name', '=', po_name]]],
                {'limit': 1})
            if not po_ids:
                st.warning(f"PO '{po_name}' not found for picking {picking['name']}")
                continue
            po_id = po_ids[0]

            # Get PO Line Items
            pol_ids = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
                'purchase.order.line', 'search',
                [[['order_id', '=', po_id]]])
            lines = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
                'purchase.order.line', 'read',
                [pol_ids],
                {'fields': ['product_template_id', 'price_unit', 'discount']})

            matched = False
            for line in lines:
                line_product = line['product_template_id'][1] if line['product_template_id'] else ''
                if sku in line_product or product_name.lower() in line_product.lower():
                    key = (po_name, line_product, vendor_name)
                    grouped_data[key]['lots'].add(ml['lot_name'])
                    grouped_data[key]['unit_price'] = line['price_unit']
                    grouped_data[key]['discount'] = line['discount']
                    matched = True
                    break

            if not matched:
                st.warning(f"No matching PO line found for product '{product_name}' (Lot: {ml['lot_name']})")

        return grouped_data

    except Exception as e:
        st.error(f"Error during lot number lookup: {str(e)}")
        return None

def create_vendor_credit(models, vendor_name, credit_note_date, due_date, reference, line_vals, company_id):
    try:
        # Fetch Vendor (Partner) ID
        vendor_ids = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'res.partner', 'search',
            [[['name', '=', vendor_name], '|', ['company_id', '=', company_id], ['company_id', '=', False]]],
            {'limit': 1})
        if not vendor_ids:
            st.error(f"Vendor '{vendor_name}' not found in company '{CONFIG['hq_company_name']}'.")
            return None

        # Fetch Journal ID (Vendor Bills / Purchase type)
        journal_ids = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'account.journal', 'search',
            [[['type', '=', 'purchase'], ['name', 'ilike', 'Vendor Bills'], ['company_id', '=', company_id]]],
            {'limit': 1})
        if not journal_ids:
            st.error("'Vendor Bills' journal not found for specified company.")
            return None

        # Create Vendor Credit Note
        credit_note_id = models.execute_kw(CONFIG['db'], st.session_state.uid, CONFIG['password'],
            'account.move', 'create',
            [{
                'move_type': 'in_refund',
                'partner_id': vendor_ids[0],
                'invoice_date': credit_note_date,
                'invoice_date_due': due_date,
                'journal_id': journal_ids[0],
                'ref': reference,
                'invoice_line_ids': line_vals,
                'company_id': company_id,
            }]
        )
        return credit_note_id
    except Exception as e:
        st.error(f"Error creating credit note: {str(e)}")
        return None

def check_login(username, password):
    """Check if login credentials match environment variables"""
    return (username == CONFIG['app_username'] and 
            password == CONFIG['app_password'] and 
            CONFIG['app_username'] and CONFIG['app_password'])

def render_login_sidebar():
    """Render login form in sidebar"""
    with st.sidebar:
        st.markdown("""
        <div style="text-align: center; padding: 20px 0;">
            <h2 style="color: #2c3e50; margin-bottom: 10px;">🔐 Login</h2>
            <p style="color: #7f8c8d; font-size: 14px;">Enter your credentials to access the system</p>
        </div>
        """, unsafe_allow_html=True)
        
        # Login form
        with st.form("login_form"):
            username = st.text_input("👤 Username", placeholder="Enter username", key="login_username")
            password = st.text_input("🔑 Password", type="password", placeholder="Enter password", key="login_password")
            login_button = st.form_submit_button("🚀 Login", use_container_width=True)
            
            if login_button:
                if check_login(username, password):
                    st.session_state.authenticated = True
                    st.session_state.username = username
                    st.success("✅ Login successful!")
                    st.rerun()
                else:
                    st.error("❌ Invalid credentials")
        
        # Show user info if logged in
        if st.session_state.get('authenticated', False):
            st.markdown("---")
            
            
            if st.button("🚪 Logout", key="logout_button", use_container_width=True):
                for key in ['authenticated', 'username', 'uid', 'models', 'grouped_data', 
                           'selected_vendor', 'selected_products']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()

def render_connection_status():
    """Render Odoo connection status and button"""
    if st.session_state.get('uid'):
        st.markdown("""
        <div style="background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); 
                    color: white; padding: 20px; border-radius: 15px; text-align: center; margin-bottom: 30px;">
            <h3 style="margin: 0; font-size: 1.5em;">🟢 Connected to Odoo</h3>
            <p style="margin: 10px 0 0 0; opacity: 0.9;">System is ready for operations</p>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style="background: linear-gradient(135deg, #ff6b6b 0%, #ee5a6f 100%); 
                    color: white; padding: 20px; border-radius: 15px; text-align: center; margin-bottom: 30px;">
            <h3 style="margin: 0; font-size: 1.5em;">🔴 Not Connected</h3>
            <p style="margin: 10px 0 0 0; opacity: 0.9;">Click the button below to connect to Odoo</p>
        </div>
        """, unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("🔗 Connect to Odoo", key="connect_odoo_button", use_container_width=True, type="primary"):
                with st.spinner("🔄 Connecting to Odoo..."):
                    st.session_state.uid, st.session_state.models = connect_odoo()
                    if st.session_state.uid:
                        st.success("✅ Successfully connected to Odoo!")
                        st.rerun()
                    else:
                        st.error("❌ Failed to connect to Odoo. Please check your credentials.")

# === Streamlit App ===
def main():
    st.set_page_config(
        page_title="Vendor Credit Note System", 
        layout="wide", 
        page_icon="📋",
        initial_sidebar_state="expanded"
    )
    
    # Initialize session state
    session_defaults = {
        'authenticated': False,
        'username': None,
        'uid': None,
        'models': None,
        'grouped_data': None,
        'selected_vendor': None,
        'selected_products': []
    }
    
    for key, default_value in session_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default_value
    
    # Enhanced CSS
    st.markdown("""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
        
        .main {
            font-family: 'Inter', sans-serif;
        }
        
        .main-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 20px;
            border-radius: 20px;
            text-align: center;
            margin-bottom: 40px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        
        .main-title {
            font-size: 3.5em;
            font-weight: 700;
            margin: 0;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        
        .main-subtitle {
            font-size: 1.2em;
            margin: 15px 0 0 0;
            opacity: 0.9;
            font-weight: 300;
        }
        
        .section-card {
            background: white;
            border-radius: 15px;
            padding: 25px;
            margin: 20px 0;
            box-shadow: 0 5px 20px rgba(0,0,0,0.08);
            border: 1px solid #f0f2f6;
        }
        
        .section-title {
            font-size: 1.8em;
            color: #2c3e50;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #3498db;
            font-weight: 600;
        }
        
        .metric-card {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            color: white;
            padding: 20px;
            border-radius: 12px;
            text-align: center;
            margin: 10px 0;
        }
        
        .lot-card {
            background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%);
            border-radius: 12px;
            padding: 20px;
            margin: 15px 0;
            border-left: 5px solid #ff6b6b;
            box-shadow: 0 3px 10px rgba(0,0,0,0.1);
        }
        
        .success-box {
            background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%);
            color: #155724;
            padding: 25px;
            border-radius: 15px;
            margin: 20px 0;
            border-left: 5px solid #28a745;
            box-shadow: 0 5px 15px rgba(40, 167, 69, 0.2);
        }
        
        .warning-box {
            background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%);
            color: #856404;
            padding: 25px;
            border-radius: 15px;
            margin: 20px 0;
            border-left: 5px solid #ffc107;
            box-shadow: 0 5px 15px rgba(255, 193, 7, 0.2);
        }
        
        .stButton > button {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 25px;
            font-weight: 600;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        
        .stButton > button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.6);
        }
        
        .upload-area {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border: 2px dashed #fff;
            border-radius: 15px;
            padding: 40px;
            text-align: center;
            color: white;
            margin: 20px 0;
        }
        
        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
        }
        
        .stTabs [data-baseweb="tab"] {
            height: 50px;
            border-radius: 25px;
            padding: 0 24px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
        }
        
        .stExpander {
            border: none;
            box-shadow: 0 3px 10px rgba(0,0,0,0.1);
            border-radius: 10px;
        }
        
        .sidebar .block-container {
            padding-top: 2rem;
        }
    </style>
    """, unsafe_allow_html=True)
    
    # Render login sidebar
    render_login_sidebar()
    
    # Main content
    if not st.session_state.get('authenticated', False):
        st.markdown("""
        <div class="main-header">
            <h1 class="main-title">📋 Vendor Credit Note System</h1>
            <p class="main-subtitle">Professional solution for managing vendor credit notes and lot tracking</p>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown("""
        <div class="section-card">
            <div style="text-align: center; padding: 60px 20px;">
                <h2 style="color: #7f8c8d; font-size: 2em; margin-bottom: 20px;">🔒 Access Required</h2>
                <p style="color: #95a5a6; font-size: 1.2em; margin-bottom: 30px;">
                    Please login using the sidebar to access the Vendor Credit Note System
                </p>
                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                           color: white; padding: 20px; border-radius: 15px; display: inline-block;">
                    <h4 style="margin: 0;">Features Include:</h4>
                    <ul style="text-align: left; margin: 15px 0 0 0;">
                        <li>📊 Bulk credit note creation</li>
                        <li>🔍 Advanced lot number lookup</li>
                        <li>📝 Manual credit note management</li>
                        <li>🔗 Direct Odoo integration</li>
                    </ul>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        return
    
    # Authenticated user content
    st.markdown("""
    <div class="main-header">
        <h1 class="main-title">📋 Vendor Credit Note System</h1>
        <p class="main-subtitle">Professional solution for managing vendor credit notes and lot tracking</p>
    </div>
    """, unsafe_allow_html=True)
    
    # Connection status
    render_connection_status()
    
    if st.session_state.uid:
        # Get HQ Company ID
        hq_company_id = get_hq_company_id(st.session_state.models)
        
        # Main Tabs
        tab1, tab2 = st.tabs(["📊 Bulk Credit Note Creation", "📝 Manual Credit Note Creation"])
        
        with tab1:
            st.markdown('<h2 class="section-title">📊 Bulk Credit Note Creation</h2>', unsafe_allow_html=True)
            
            # Upload section
            st.markdown("""
            <div style="background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%); 
                       padding: 25px; border-radius: 15px; margin: 20px 0; text-align: center;">
                <h3 style="color: #2c3e50; margin-bottom: 15px;">📁 Upload Excel File</h3>
                <p style="color: #34495e; margin-bottom: 0;">Upload your Excel file with lot numbers in Column A</p>
            </div>
            """, unsafe_allow_html=True)
            
            uploaded_file = st.file_uploader(
                "Choose Excel file", 
                type=["xlsx", "xls"], 
                help="Excel file should contain lot numbers in the first column (Column A)",
                key="bulk_upload_file"
            )
            
            if uploaded_file:
                try:
                    df = pd.read_excel(uploaded_file)
                    if df.empty:
                        st.warning("⚠️ The uploaded file is empty.")
                    else:
                        lot_numbers = df.iloc[:, 0].astype(str).str.strip().str.upper().tolist()
                        
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.markdown(f"""
                            <div class="metric-card">
                                <h3 style="margin: 0;">📋 Lot Numbers</h3>
                                <h2 style="margin: 10px 0 0 0;">{len(lot_numbers)}</h2>
                            </div>
                            """, unsafe_allow_html=True)
                        
                        # Credit Note Details
                        st.markdown('<h3 style="color: #2c3e50; margin-top: 30px;">📅 Credit Note Details</h3>', unsafe_allow_html=True)
                        
                        today = datetime.now().date()
                        due_date_default = today + timedelta(days=30)
                        
                        col1, col2 = st.columns(2)
                        with col1:
                            credit_note_date = st.date_input("📅 Credit Note Date:", value=today, key="bulk_credit_date")
                        with col2:
                            due_date = st.date_input("⏰ Due Date:", value=due_date_default, key="bulk_due_date")
                        
                        reference = st.text_input("📝 Reference/Reason:", value="Damage", help="Enter the reason for the credit note", key="bulk_reference")
                        
                        col1, col2, col3 = st.columns([1, 2, 1])
                        with col2:
                            if st.button("🚀 Process & Create Credit Note", key="bulk_process_button", use_container_width=True, type="primary"):
                                with st.spinner("🔄 Processing lot numbers and creating credit note..."):
                                    # Process the lot numbers (existing logic)
                                    grouped_data = lookup_lot_numbers(lot_numbers, st.session_state.models, hq_company_id)
                                    
                                    if grouped_data:
                                        vendors = list(set([key[2] for key in grouped_data.keys()]))
                                        
                                        for vendor_name in vendors:
                                            st.markdown(f"### Processing vendor: {vendor_name}")
                                            vendor_data = {k: v for k, v in grouped_data.items() if k[2] == vendor_name}
                                            
                                            # Create line values for this vendor
                                            line_vals = []
                                            for (po_name, product_name, _), data in vendor_data.items():
                                                if len(data['lots']) == 0:
                                                    continue
                                                
                                                product_ids = st.session_state.models.execute_kw(
                                                    CONFIG['db'], st.session_state.uid, CONFIG['password'],
                                                    'product.product', 'search',
                                                    [[['name', 'ilike', product_name], '|', 
                                                    ['company_id', '=', hq_company_id], ['company_id', '=', False]]],
                                                    {'limit': 1})
                                                
                                                if product_ids:
                                                    line_vals.append((0, 0, {
                                                        'product_id': product_ids[0],
                                                        'quantity': len(data['lots']),
                                                        'price_unit': data['unit_price'],
                                                        'discount': data['discount'],
                                                        'name': f"Damage - Lots: {', '.join(sorted(data['lots'])[:3])}" + ("..." if len(data['lots']) > 3 else ""),
                                                    }))
                                            
                                            if line_vals:
                                                credit_note_id = create_vendor_credit(
                                                    st.session_state.models,
                                                    vendor_name,
                                                    credit_note_date.strftime('%Y-%m-%d'),
                                                    due_date.strftime('%Y-%m-%d'),
                                                    reference,
                                                    line_vals,
                                                    hq_company_id
                                                )
                                                
                                                if credit_note_id:
                                                    st.markdown(f"""
                                                    <div class="success-box">
                                                        <h3 style="margin: 0 0 15px 0;">✅ Credit Note Created Successfully!</h3>
                                                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                                                            <div><strong>🆔 Credit Note ID:</strong> {credit_note_id}</div>
                                                            <div><strong>🏪 Vendor:</strong> {vendor_name}</div>
                                                            <div><strong>📅 Date:</strong> {credit_note_date.strftime('%Y-%m-%d')}</div>
                                                            <div><strong>📝 Reference:</strong> {reference}</div>
                                                        </div>
                                                        <div style="margin-top: 15px; text-align: center;">
                                                            <strong>📦 Total Products:</strong> {len(line_vals)}
                                                        </div>
                                                    </div>
                                                    """, unsafe_allow_html=True)
                                            else:
                                                st.error("❌ No valid products found to create credit note.")
                except Exception as e:
                    st.error(f"❌ Error processing file: {str(e)}")
            
            st.markdown('</div>', unsafe_allow_html=True)
        
        with tab2:
            st.markdown('<h2 class="section-title">📝 Manual Credit Note Creation</h2>', unsafe_allow_html=True)
            
            # Lot Number Lookup Section
            st.markdown('<h3 class="section-title">🔍 Lot Number Lookup</h3>', unsafe_allow_html=True)
            
            
            lot_numbers = []
            
            
            lot_input = st.text_area(
                "Enter Lot Numbers (comma-separated):", 
                placeholder="Enter lot numbers separated by commas, e.g., LOT001, LOT002, LOT003",
                height=100,
                key="manual_lot_input"
            )
                
            if lot_input:
                lot_numbers = [lot.strip().upper() for lot in lot_input.split(',') if lot.strip()]
            
            if lot_numbers:
                if st.button("🔍 Lookup Lot Numbers", key="manual_lookup_button", use_container_width=True):
                    with st.spinner("🔄 Searching for lot numbers..."):
                        st.session_state.grouped_data = lookup_lot_numbers(lot_numbers, st.session_state.models, hq_company_id)
                        if st.session_state.grouped_data:
                            st.success("✅ Lot numbers processed successfully!")
                        else:
                            st.warning("No matching lot numbers found.")
            
            # Display lookup results
            if st.session_state.grouped_data:
                st.markdown('<h3 class="section-title">🔎 Lookup Results</h3>', unsafe_allow_html=True)
                
                # Collect all vendors from results
                vendors = list(set([key[2] for key in st.session_state.grouped_data.keys()]))
                
                if len(vendors) > 1:
                    st.session_state.selected_vendor = st.selectbox("Select Vendor:", vendors, key="manual_vendor_select")
                else:
                    st.session_state.selected_vendor = vendors[0]
                    st.info(f"Vendor: {st.session_state.selected_vendor}")
                
                # Filter data for selected vendor
                vendor_data = {k: v for k, v in st.session_state.grouped_data.items() 
                              if k[2] == st.session_state.selected_vendor}
                
                # Display results for selected vendor
                for (po_name, product_name, vendor_name), data in vendor_data.items():
                    with st.expander(f"📋 PO: {po_name} | 🧵 Product: {product_name}", expanded=False):
                        st.markdown(f"""
                        <div class="lot-card">
                            <h4 style="color: #2c3e50; margin: 0 0 15px 0;">Product Details</h4>
                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                                <div><strong>📋 PO:</strong> {po_name}</div>
                                <div><strong>🏪 Vendor:</strong> {vendor_name}</div>
                                <div><strong>🧵 Product:</strong> {product_name}</div>
                                <div><strong>🔢 Lot Count:</strong> {len(data['lots'])}</div>
                                <div><strong>💰 Unit Price:</strong> ₹{data['unit_price']:,.2f}</div>
                                <div><strong>🎯 Discount:</strong> {data['discount']}%</div>
                            </div>
                            <div style="margin-top: 15px;">
                                <strong>🏷️ Lot Numbers:</strong><br>
                                <span style="font-family: monospace; background: #f8f9fa; padding: 5px; border-radius: 5px;">
                                    {', '.join(sorted(data['lots']))}
                                </span>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                        
                        if st.button(
                            f"➕ Add to Credit Note", 
                            key=f"add_{po_name}_{product_name}", 
                            use_container_width=True
                        ):
                            new_product = {
                                'po_name': po_name,
                                'product_name': product_name,
                                'lots': sorted(data['lots']),
                                'count': len(data['lots']),
                                'unit_price': data['unit_price'],
                                'discount': data['discount']
                            }
                            
                            # Check if product already exists
                            existing = False
                            for existing_product in st.session_state.selected_products:
                                if (existing_product['po_name'] == po_name and 
                                    existing_product['product_name'] == product_name):
                                    existing = True
                                    break
                            
                            if not existing:
                                st.session_state.selected_products.append(new_product)
                                st.success(f"✅ Added {product_name} to credit note!")
                                st.rerun()
                            else:
                                st.warning("⚠️ Product already in credit note list!")
            
            # Create Credit Note Section
            st.markdown('<h3 class="section-title">📝 Create Credit Note</h3>', unsafe_allow_html=True)
            
            if not st.session_state.selected_vendor and not st.session_state.grouped_data:
                st.warning("Please perform a lot number lookup first to select products.")
            else:
                # Default dates
                today = datetime.now().date()
                due_date_default = today + timedelta(days=30)
                
                # Vendor Info
                if st.session_state.selected_vendor:
                    st.info(f"🏪 Vendor: {st.session_state.selected_vendor}")
                else:
                    st.session_state.selected_vendor = st.text_input("🏪 Enter Vendor Name:", key="manual_vendor_input")
                
                # Credit Note Details
                col1, col2 = st.columns(2)
                with col1:
                    credit_note_date = st.date_input("📅 Credit Note Date:", value=today, key="manual_credit_date")
                with col2:
                    due_date = st.date_input("⏰ Due Date:", value=due_date_default, key="manual_due_date")
                
                reference = st.text_input("📝 Reference/Reason:", value="Damage", key="manual_reference")
                
                # Selected Products
                if st.session_state.selected_products:
                    st.markdown('<h3 class="section-title">🛒 Selected Products</h3>', unsafe_allow_html=True)
                    
                    total_amount = 0
                    
                    for idx, product in enumerate(st.session_state.selected_products):
                        with st.expander(f"📦 {product['product_name']} (Qty: {product['count']})", expanded=True):
                            col1, col2, col3 = st.columns([2, 1, 1])
                            
                            with col1:
                                st.markdown(
                                    f"""
                                    <div class="lot-card">
                                        <h4 style="color: #2c3e50; margin: 0 0 15px 0;">Product Information</h4>
                                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                                            <div><strong>📋 PO:</strong> {product['po_name']}</div>
                                            <div><strong>🧵 Product:</strong> {product['product_name']}</div>
                                            <div><strong>🔢 Quantity:</strong> {product['count']}</div>
                                            <div><strong>💰 Unit Price:</strong> ₹{product['unit_price']:,.2f}</div>
                                            <div><strong>🎯 Discount:</strong> {product['discount']}%</div>
                                            <div><strong>💵 Line Total:</strong> ₹{(product['unit_price'] * product['count'] * (1 - product['discount']/100)):,.2f}</div>
                                            <div><strong>🏷️ Lots:</strong> {', '.join(product['lots'][:5])}</div>
                                        </div>
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )

                                total_amount += product['unit_price'] * product['count'] * (1 - product['discount']/100)
                            
                            with col2:
                                # Quantity adjustment
                                new_qty = st.number_input(
                                    "Adjust Quantity",
                                    min_value=1,
                                    max_value=len(product['lots']),
                                    value=product['count'],
                                    key=f"qty_{idx}",
                                    help="Adjust the quantity for this product"
                                )
                                
                                if new_qty != product['count']:
                                    st.session_state.selected_products[idx]['count'] = new_qty
                                    st.rerun()
                            
                            with col3:
                                if st.button(f"🗑️ Remove", key=f"remove_{idx}", use_container_width=True):
                                    st.session_state.selected_products.pop(idx)
                                    st.success("✅ Product removed!")
                                    st.rerun()
                    
                    # Total summary
                    st.markdown(f"""
                    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                               color: white; padding: 25px; border-radius: 15px; text-align: center; margin: 30px 0;">
                        <h3 style="margin: 0 0 15px 0;">💰 Credit Note Summary</h3>
                        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;">
                            <div>
                                <h4 style="margin: 0;">📦 Total Products</h4>
                                <h2 style="margin: 5px 0 0 0;">{len(st.session_state.selected_products)}</h2>
                            </div>
                            <div>
                                <h4 style="margin: 0;">🔢 Total Items</h4>
                                <h2 style="margin: 5px 0 0 0;">{sum(p['count'] for p in st.session_state.selected_products)}</h2>
                            </div>
                            <div>
                                <h4 style="margin: 0;">💵 Total Amount</h4>
                                <h2 style="margin: 5px 0 0 0;">₹{total_amount:,.2f}</h2>
                            </div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    # Create Credit Note Button
                    col1, col2, col3 = st.columns([1, 2, 1])
                    with col2:
                        if st.button(
                            "🎯 Create Vendor Credit Note", 
                            key="manual_create_button", 
                            use_container_width=True, 
                            type="primary"
                        ):
                            with st.spinner("🔄 Creating credit note..."):
                                # Prepare line values for Odoo
                                line_vals = []
                                for product in st.session_state.selected_products:
                                    # Find product ID
                                    product_ids = st.session_state.models.execute_kw(
                                        CONFIG['db'], st.session_state.uid, CONFIG['password'],
                                        'product.product', 'search',
                                        [[['name', 'ilike', product['product_name']], '|', 
                                          ['company_id', '=', hq_company_id], ['company_id', '=', False]]],
                                        {'limit': 1})
                                    
                                    if product_ids:
                                        line_vals.append((0, 0, {
                                            'product_id': product_ids[0],
                                            'quantity': product['count'],
                                            'price_unit': product['unit_price'],
                                            'discount': product['discount'],
                                            'name': f"Damage - Lots: {', '.join(product['lots'][:3])}" + ("..." if len(product['lots']) > 3 else ""),
                                        }))
                                
                                # Create Credit Note
                                credit_note_id = create_vendor_credit(
                                    st.session_state.models,
                                    st.session_state.selected_vendor,
                                    credit_note_date.strftime('%Y-%m-%d'),
                                    due_date.strftime('%Y-%m-%d'),
                                    reference,
                                    line_vals,
                                    hq_company_id
                                )
                                
                                if credit_note_id:
                                    st.markdown(f"""
                                    <div class="success-box">
                                        <h3 style="margin: 0 0 20px 0;">🎉 Credit Note Created Successfully!</h3>
                                        <div style="background: rgba(255,255,255,0.2); padding: 20px; border-radius: 10px;">
                                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                                                <div><strong>🆔 Credit Note ID:</strong> {credit_note_id}</div>
                                                <div><strong>🏪 Vendor:</strong> {st.session_state.selected_vendor}</div>
                                                <div><strong>📅 Date:</strong> {credit_note_date.strftime('%Y-%m-%d')}</div>
                                                <div><strong>⏰ Due Date:</strong> {due_date.strftime('%Y-%m-%d')}</div>
                                                <div><strong>📝 Reference:</strong> {reference}</div>
                                                <div><strong>💵 Total Amount:</strong> ₹{total_amount:,.2f}</div>
                                            </div>
                                        </div>
                                        <div style="text-align: center; margin-top: 20px;">
                                            <p style="margin: 0; font-size: 1.1em;">The credit note has been created in Odoo and is ready for processing.</p>
                                        </div>
                                    </div>
                                    """, unsafe_allow_html=True)
                                    
                                    # Clear selected products after successful creation
                                    st.session_state.selected_products = []
                                    st.session_state.grouped_data = None
                                    st.balloons()
                                    st.rerun()
                else:
                    st.markdown("""
                    <div style="background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%); 
                               color: #856404; padding: 25px; border-radius: 15px; text-align: center; margin: 20px 0;">
                        <h3 style="margin: 0 0 10px 0;">⚠️ No Products Selected</h3>
                        <p style="margin: 0;">Please add products from the lookup results above to create a credit note.</p>
                    </div>
                    """, unsafe_allow_html=True)
            
            st.markdown('</div>', unsafe_allow_html=True)
    
    # Footer
    st.markdown("""
    <div style="margin-top: 60px; padding: 30px; text-align: center; 
               background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
               color: white; border-radius: 15px;">
        <h3 style="margin: 0 0 15px 0;">📋 Vendor Credit Note System</h3>
        <p style="margin: 0; opacity: 0.8;">Professional solution for managing vendor credits with Odoo integration</p>
        <p style="margin: 10px 0 0 0; font-size: 0.9em; opacity: 0.6;">
            Powered by Streamlit • Connected to Odoo • Secure & Reliable
        </p>
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
