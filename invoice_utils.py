import datetime
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.lib.units import inch, mm
from reportlab.pdfgen import canvas
import os
from io import BytesIO
import requests 
import json
from database import supabase # Make sure supabase is imported

# --- *** NEW HELPER FUNCTION *** ---
def _clean_string(s, default=''):
    """
    Forcefully cleans a string by stripping whitespace and removing
    any non-ASCII characters that can corrupt email headers.
    """
    if s is None:
        return default
    try:
        # Encode to ASCII, ignoring errors, then decode back.
        # This strips all non-ASCII chars. Then strip whitespace.
        return s.encode('ascii', 'ignore').decode('ascii').strip()
    except Exception:
        # Fallback for any other error
        return str(s).strip()
# --- *** END OF NEW FUNCTION *** ---

def get_placeholder_isp_details():
    """Returns dummy ISP company details if DB fetch fails."""
    print("WARNING: Using placeholder ISP Company details for PDF.")
    return {
        "company_name": "Your ISP Name (Configure)", "address": "123 Street",
        "phone": "+123", "email": "billing@isp.com",
        "logo_path": None, "payment_info": "bKash: 01x (Configure)",
        "smtp_host": None, "smtp_port": 587, "smtp_user": None, "smtp_pass": None,
        "sender_name": "Your ISP"
    }

def get_isp_company_details_from_db(company_id):
    """
    Fetches and CLEANS ISP company details, including SMTP info, from the database.
    Now accepts a company_id parameter for Flask.
    """
    if not company_id or not supabase:
        return get_placeholder_isp_details()
    try:
        response = supabase.table('isp_companies')\
                           .select('company_name, company_details, payment_info, logo_url')\
                           .eq('id', company_id)\
                           .maybe_single()\
                           .execute()
                            
        if response and response.data:
            details = response.data
            company_info = details.get('company_details', {})
            if company_info is None:
                company_info = {}
            
            # --- Clean all strings coming from the database ---
            company_name_cleaned = _clean_string(details.get('company_name'), 'N/A')
            
            print(f"--- DEBUG: Cleaning sender_name. Original: '{details.get('company_name')}', Cleaned: '{company_name_cleaned}' ---")

            return {
                "company_name": company_name_cleaned,
                "address": _clean_string(company_info.get('address'), 'N/A'),
                "phone": _clean_string(company_info.get('phone'), 'N/A'),
                "email": _clean_string(company_info.get('email'), 'N/A'),
                "logo_path": details.get('logo_url', None), 
                "payment_info": _clean_string(details.get('payment_info'), ''),
                "smtp_host": _clean_string(company_info.get('smtp_host'), None),
                "smtp_port": company_info.get('smtp_port', 587),
                "smtp_user": _clean_string(company_info.get('smtp_user'), None),
                "smtp_pass": _clean_string(company_info.get('smtp_pass'), None),
                "sender_name": company_name_cleaned 
            }
        else:
            return get_placeholder_isp_details()
    except Exception as e:
        print(f"ERROR: Failed to fetch ISP details from DB: {e}. Using placeholders.")
        return get_placeholder_isp_details()

def generate_invoice_number(customer_id, issue_date):
    """Generates a unique invoice number."""
    cust_short = str(customer_id).split('-')[0].upper()
    date_str = issue_date.strftime('%Y%m%d')
    return f"INV-{cust_short}-{date_str}"


def draw_paid_watermark(canvas, doc, isp_details):
    """Draws a round 'PAID' stamp with company name."""
    canvas.saveState()
    x_center, y_center = letter[0] / 2, letter[1] / 2
    radius = 1.2 * inch
    circle_color = colors.Color(0, 0.5, 0, alpha=0.15)
    text_color = colors.Color(0, 0.4, 0, alpha=0.2)
    paid_font_size = 50
    name_font_size = 10
    
    canvas.setStrokeColor(circle_color)
    canvas.setFillColor(text_color)
    canvas.setLineWidth(2)
    canvas.circle(x_center, y_center, radius, stroke=1, fill=0)
    canvas.setFont('Helvetica-Bold', paid_font_size)
    canvas.drawCentredString(x_center, y_center - (paid_font_size * 0.35), "PAID")
    
    company_name = isp_details.get('company_name', 'Your ISP')
    canvas.setFont('Helvetica-Bold', name_font_size)
    canvas.setFillColor(circle_color)
    canvas.drawCentredString(x_center, y_center - (paid_font_size * 0.35) - name_font_size*1.5 , company_name.upper())
    canvas.restoreState()

def create_receipt_pdf_as_bytes(invoice_data, customer_data, company_data, generated_by_name="Customer Portal", charge_details=None, employee_name=None):
    """Generates a PDF receipt and returns it as bytes."""
    try:
        buffer = BytesIO()
        
        # company_data is now the full details dict
        isp_details = company_data 
        
        doc = SimpleDocTemplate(buffer, pagesize=letter,
                                leftMargin=0.75*inch, rightMargin=0.75*inch,
                                topMargin=0.75*inch, bottomMargin=0.75*inch)
        styles = getSampleStyleSheet()
        story = []

        logo_path = isp_details.get("logo_path")
        logo_image = None
        if logo_path:
            try:
                response = requests.get(logo_path)
                response.raise_for_status() 
                logo_data = BytesIO(response.content)
                
                img = Image(logo_data, width=1.3*inch) 
                aspect = img.imageHeight / img.imageWidth
                img.drawHeight = (1.3 * inch) * aspect
                logo_image = img
                
                logo_image.hAlign = 'LEFT'
            except Exception as e:
                print(f"Error downloading logo: {e}")
                logo_image = None

        company_name_style = ParagraphStyle(name='CompanyName', parent=styles['h2'], alignment=TA_RIGHT)
        company_details_style = ParagraphStyle(name='CompanyDetails', parent=styles['Normal'], alignment=TA_RIGHT, leading=14)
        
        company_name_para = Paragraph(isp_details['company_name'], company_name_style)
        company_details_para = Paragraph(
            f"{isp_details['address']}<br/>"
            f"{isp_details['phone']} | {isp_details['email']}",
            company_details_style
        )

        if logo_image:
            header_data = [[logo_image, [company_name_para, company_details_para]]]
            header_table = Table(header_data, colWidths=[1.5*inch, None])
            header_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
            story.append(header_table)
        else:
            story.append(company_name_para)
            story.append(company_details_para)

        story.append(Spacer(1, 0.25 * inch))
        story.append(Table(
            [['']], 
            colWidths='100%', 
            style=TableStyle([('LINEBELOW', (0,0), (-1,-1), 2, colors.blue)])))
        story.append(Spacer(1, 0.25 * inch))

        try:
            issue_date = datetime.datetime.fromisoformat(invoice_data['issue_date'])
            billing_period = issue_date.strftime('%B %Y')
        except:
            billing_period = "N/A"
            
        title_style = styles['h1']; title_style.alignment = TA_LEFT
        story.append(Paragraph(f"Payment Receipt: {billing_period}", title_style))
        story.append(Spacer(1, 0.3 * inch))

        styles['Normal'].leading = 14
        p_style_left = ParagraphStyle(name='Left', parent=styles['Normal'], alignment=TA_LEFT)
        p_style_right = ParagraphStyle(name='Right', parent=styles['Normal'], alignment=TA_RIGHT)
        
        payment_date_str = datetime.date.today().strftime('%d %b %Y')
        if invoice_data.get('paid_at'):
             try: payment_date_str = datetime.datetime.fromisoformat(invoice_data['paid_at']).strftime('%d %b %Y')
             except: pass
        elif invoice_data.get('updated_at'): 
             try: payment_date_str = datetime.datetime.fromisoformat(invoice_data['updated_at']).strftime('%d %b %Y')
             except: pass

        billed_to_content = f"""
            <b>Billed To:</b><br/>
            {customer_data.get('full_name', 'N/A')}<br/>
            {customer_data.get('address', '').replace(os.linesep, '<br/>')}<br/>
            Phone: {customer_data.get('phone_number', 'N/A')}<br/>
            Email: {customer_data.get('email', 'N/A')}
        """
        
        employee_name_str = employee_name or 'N/A'
        receipt_details_content = f"""
            <b>Receipt Details:</b><br/>
            Receipt Number: RCPT-{invoice_data['invoice_number']}<br/>
            Invoice Number: {invoice_data['invoice_number']}<br/>
            <b>Serviced By: {employee_name_str}</b><br/>
            Payment Date: {payment_date_str}<br/>
            Billing Period: {billing_period}
        """

        details_data = [[Paragraph(billed_to_content, p_style_left), Paragraph(receipt_details_content, p_style_right)]]
        details_table = Table(details_data, colWidths=['60%', '40%'])
        details_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(details_table)
        story.append(Spacer(1, 0.3 * inch))

        bold_left = ParagraphStyle(name='BoldLeft', parent=styles['Normal'], fontName='Helvetica-Bold', alignment=TA_LEFT)
        bold_right = ParagraphStyle(name='BoldRight', parent=styles['Normal'], fontName='Helvetica-Bold', alignment=TA_RIGHT)
        
        summary_data = [
            [Paragraph('<b>Description</b>', styles['Normal']), Paragraph('<b>Amount (BDT)</b>', p_style_right)],
        ]

        if charge_details:
            for item in charge_details:
                summary_data.append([item.get('item', 'N/A'), f"{item.get('cost', 0.00):.2f}"])
        else:
            description = f"Payment for Invoice {invoice_data['invoice_number']}"
            pkg_details = invoice_data.get('package_details')
            if isinstance(pkg_details, str):
                try: pkg_details = json.loads(pkg_details)
                except: pkg_details = {}
            if isinstance(pkg_details, dict):
                pkg_name = pkg_details.get('name', 'Internet Service')
                description = f"Package: {pkg_name} ({billing_period})"
            summary_data.append([description, f"{invoice_data['amount']:.2f}"])
            
        summary_data.append(
            [Paragraph('Total Amount', bold_left), Paragraph(f"{invoice_data['amount']:.2f}", bold_right)]
        )

        summary_table = Table(summary_data, colWidths=['75%', '25%'])
        style_commands = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4F81BD')), 
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke), 
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'), 
            ('ALIGN', (0, 0), (0, -1), 'LEFT'), 
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), 
            ('GRID', (0, 0), (-1, -1), 1, colors.black), 
            ('BOX', (0, 0), (-1, -1), 1, colors.black), 
            ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#E0E0E0')),
        ]
        
        summary_table.setStyle(TableStyle(style_commands))
        story.append(summary_table)
        story.append(Spacer(1, 0.3 * inch))

        net_paid_style = ParagraphStyle(name='NetPaid', parent=styles['h3'], alignment=TA_RIGHT,
                                        textColor=colors.whitesmoke, backColor=colors.HexColor('#00B050'),
                                        padding=10, borderPadding=5, borderRadius=5)
        
        story.append(Paragraph(f"Total Paid: {invoice_data['amount']:.2f} BDT", net_paid_style))
        story.append(Spacer(1, 0.2 * inch))

        if isp_details.get('payment_info'):
            notes_style = styles['Normal']; notes_style.alignment = TA_CENTER
            story.append(Paragraph(f"<i>Payment Instructions: {isp_details['payment_info']}</i>", notes_style))
            story.append(Spacer(1, 0.3 * inch))

        footer_style = ParagraphStyle(name='Footer', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER)
        story.append(Paragraph(f"Generated by: {generated_by_name}", footer_style))
        story.append(Paragraph("Powered by Nazrul Huda", footer_style))

        doc.build(story, onFirstPage=lambda c, d: draw_paid_watermark(c, d, isp_details))
        
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        print(f"Receipt PDF generated in memory for Invoice {invoice_data['invoice_number']}")
        return True, pdf_bytes

    except Exception as e:
        print(f"Error generating PDF: {e}")
        return False, str(e)


def create_payslip_pdf_as_bytes(payroll_data, employee_data, company_data, generated_by_name="System"):
    """Generates a PDF payslip and returns it as bytes."""
    try:
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter,
                                leftMargin=0.75*inch, rightMargin=0.75*inch,
                                topMargin=0.75*inch, bottomMargin=0.75*inch)
        styles = getSampleStyleSheet()
        story = []

        # company_data is now the full details dict
        isp_details = company_data

        logo_path = isp_details.get("logo_path")
        logo_image = None
        if logo_path:
            try:
                response = requests.get(logo_path)
                response.raise_for_status() 
                logo_data = BytesIO(response.content)
                img = Image(logo_data, width=1.3*inch) 
                aspect = img.imageHeight / img.imageWidth
                img.drawHeight = (1.3 * inch) * aspect
                logo_image = img
                logo_image.hAlign = 'LEFT'
            except Exception as e:
                print(f"Error downloading logo: {e}")
                logo_image = None

        company_name_style = ParagraphStyle(name='CompanyName', parent=styles['h2'], alignment=TA_RIGHT)
        company_details_style = ParagraphStyle(name='CompanyDetails', parent=styles['Normal'], alignment=TA_RIGHT, leading=14)
        
        company_name_para = Paragraph(isp_details['company_name'], company_name_style)
        company_details_para = Paragraph(
            f"{isp_details['address']}<br/>"
            f"{isp_details['phone']} | {isp_details['email']}",
            company_details_style
        )

        if logo_image:
            header_data = [[logo_image, [company_name_para, company_details_para]]]
            header_table = Table(header_data, colWidths=[1.5*inch, None])
            header_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
            story.append(header_table)
        else:
            story.append(company_name_para)
            story.append(company_details_para)

        story.append(Spacer(1, 0.25 * inch))
        story.append(Table([['']], colWidths='100%', style=TableStyle([('LINEBELOW', (0,0), (-1,-1), 2, colors.blue)])))
        story.append(Spacer(1, 0.25 * inch))

        title_style = styles['h1']
        title_style.alignment = TA_LEFT
        pay_period = f"{datetime.date(payroll_data['pay_period_year'], payroll_data['pay_period_month'], 1).strftime('%B %Y')}"
        story.append(Paragraph(f"Payslip: {pay_period}", title_style))
        story.append(Spacer(1, 0.2 * inch))

        p_style_left = ParagraphStyle(name='Left', parent=styles['Normal'], alignment=TA_LEFT, leading=14)
        p_style_right = ParagraphStyle(name='Right', parent=styles['Normal'], alignment=TA_RIGHT, leading=14)
        
        employee_info = f"""
            <b>Paid To:</b><br/>
            {employee_data.get('full_name', 'N/A')}<br/>
            {employee_data.get('email', 'N/A')}<br/>
            <b>Role:</b> {employee_data.get('role', 'N/A')}
        """
        
        payslip_num = f"PAY-{payroll_data['id'][:8].upper()}"
        pay_details = f"""
            <b>Payslip Details:</b><br/>
            <b>Payslip Number:</b> {payslip_num}<br/>
            <b>Generated On:</b> {datetime.datetime.now().strftime('%d %b %Y')}<br/>
            <b>Payment Status:</b> {payroll_data['status']}<br/>
            <b>Pay Period:</b> {pay_period}
        """
        
        details_data = [[Paragraph(employee_info, p_style_left), Paragraph(pay_details, p_style_right)]]
        details_table = Table(details_data, colWidths=['50%', '50%'])
        details_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(details_table)
        story.append(Spacer(1, 0.3 * inch))

        bold_left = ParagraphStyle(name='BoldLeft', parent=styles['Normal'], fontName='Helvetica-Bold', alignment=TA_LEFT)
        bold_right = ParagraphStyle(name='BoldRight', parent=styles['Normal'], fontName='Helvetica-Bold', alignment=TA_RIGHT)

        gross_salary = payroll_data['base_salary'] + payroll_data['incentives'] + payroll_data['increments']
        total_deductions = payroll_data['deductions']

        summary_data = [
            [Paragraph('<b>Description</b>', styles['Normal']), Paragraph('<b>Amount (BDT)</b>', p_style_right)],
            ['Base Salary', f"{payroll_data['base_salary']:.2f}"],
            ['Incentives', f"{payroll_data['incentives']:.2f}"],
            ['Increments', f"{payroll_data['increments']:.2f}"],
            [Paragraph('Gross Salary', bold_left), Paragraph(f"{gross_salary:.2f}", bold_right)],
            ['Deductions', f"{total_deductions:.2f}"],
            [Paragraph('Total Deductions', bold_left), Paragraph(f"{total_deductions:.2f}", bold_right)],
        ]

        summary_table = Table(summary_data, colWidths=['75%', '25%'])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4F81BD')), 
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke), 
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'), 
            ('ALIGN', (0, 0), (0, -1), 'LEFT'), 
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), 
            ('GRID', (0, 0), (-1, -1), 1, colors.black), 
            ('BOX', (0, 0), (-1, -1), 1, colors.black), 
            ('LINEABOVE', (0, 4), (-1, 4), 1, colors.black),
            ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#E0E0E0')),
            ('LINEABOVE', (0, 6), (-1, 6), 1, colors.black),
            ('BACKGROUND', (0, 6), (-1, 6), colors.HexColor('#E0E0E0')),
        ]))
        
        story.append(summary_table)
        story.append(Spacer(1, 0.3 * inch))

        net_salary = payroll_data['net_salary']
        net_salary_style = ParagraphStyle(name='NetSalary', parent=styles['h3'], alignment=TA_RIGHT,
                                        textColor=colors.whitesmoke, backColor=colors.HexColor('#00B050'),
                                        padding=10, borderPadding=5, borderRadius=5)
        
        story.append(Paragraph(f"Net Salary Paid: {net_salary:.2f} BDT", net_salary_style))
        story.append(Spacer(1, 0.5 * inch))
        
        footer_style = ParagraphStyle(name='Footer', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER)
        
        story.append(Paragraph(f"This is a system-generated payslip. Generated by: {generated_by_name}", footer_style))
        story.append(Paragraph("Powered by Nazrul Huda", footer_style))

        doc.build(story)
        
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        print(f"Payslip PDF generated in memory for {employee_data.get('full_name')}")
        return True, pdf_bytes

    except Exception as e:
        print(f"Error generating payslip PDF: {e}")
        return False, str(e)


def create_thermal_receipt_as_bytes(invoice_data, customer_data, company_data, generated_by):
    """Creates a small, simple receipt for thermal printers and returns as bytes."""
    try:
        buffer = BytesIO()
        
        width, height = 80 * mm, 150 * mm 
        doc = SimpleDocTemplate(buffer, pagesize=(width, height),
                                leftMargin=5*mm, rightMargin=5*mm,
                                topMargin=5*mm, bottomMargin=5*mm)
        
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name='Center', alignment=TA_CENTER, fontName="Helvetica", fontSize=10))
        styles.add(ParagraphStyle(name='CenterBold', alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=12))
        styles.add(ParagraphStyle(name='Left', alignment=TA_LEFT, fontName="Helvetica", fontSize=10, leading=12))
        styles.add(ParagraphStyle(name='Right', alignment=TA_RIGHT, fontName="Helvetica", fontSize=10, leading=12))
        styles.add(ParagraphStyle(name='Total', alignment=TA_RIGHT, fontName="Helvetica-Bold", fontSize=14))
        
        elements = []
        
        # company_data is now the full details dict
        isp_details = company_data
        company_name = isp_details.get('company_name', 'Your ISP')
        address = isp_details.get('address', 'N/A')
        phone = isp_details.get('phone', 'N/A')

        # --- *** NEW LOGO LOGIC *** ---
        logo_path = isp_details.get("logo_path")
        logo_image = None
        if logo_path:
            try:
                response = requests.get(logo_path)
                response.raise_for_status() 
                logo_data = BytesIO(response.content)
                
                # Resize for thermal paper (40mm width)
                img = Image(logo_data, width=30*mm) 
                aspect = img.imageHeight / img.imageWidth
                img.drawHeight = (40*mm) * aspect
                logo_image = img
                
                logo_image.hAlign = 'CENTER' # Center the logo
            except Exception as e:
                print(f"Error downloading logo for thermal receipt: {e}")
                logo_image = None
        
        if logo_image:
            elements.append(logo_image)
            elements.append(Spacer(1, 2 * mm)) # Add a small space
        # --- *** END OF NEW LOGIC *** ---

        elements.append(Paragraph(company_name, styles['CenterBold']))
        elements.append(Paragraph(address.replace('\n', '<br/>'), styles['Center']))
        elements.append(Paragraph(f"Phone: {phone}", styles['Center']))
        elements.append(Spacer(1, 4 * mm))
        elements.append(Paragraph("--- PAYMENT RECEIPT ---", styles['CenterBold']))
        elements.append(Spacer(1, 4 * mm))

        paid_at = datetime.datetime.fromisoformat(invoice_data.get('paid_at', invoice_data['created_at'])).strftime('%d %b %Y, %I:%M %p')
        pay_period = datetime.datetime.fromisoformat(invoice_data['issue_date']).strftime('%B %Y')

        elements.append(Paragraph(f"<b>Customer:</b> {customer_data.get('full_name', 'N/A')}", styles['Left']))
        elements.append(Paragraph(f"<b>Invoice #:</b> {invoice_data.get('invoice_number', 'N/A')}", styles['Left']))
        elements.append(Paragraph(f"<b>Payment Date:</b> {paid_at}", styles['Left']))
        elements.append(Paragraph(f"<b>Payment Method:</b> {invoice_data.get('payment_method', 'N/A')}", styles['Left']))
        elements.append(Paragraph(f"<b>Transaction ID:</b> {invoice_data.get('transaction_id', 'N/A')}", styles['Left']))
        elements.append(Paragraph(f"<b>Received By:</b> {generated_by}", styles['Left']))
        
        elements.append(Spacer(1, 4 * mm))
        elements.append(Paragraph("--------------------------------------------------", styles['Center']))
        elements.append(Spacer(1, 2 * mm))

        pkg_details = invoice_data.get('package_details', {})
        if isinstance(pkg_details, str):
            try: pkg_details = json.loads(pkg_details)
            except: pkg_details = {}
        
        desc = f"Monthly Bill: {pkg_details.get('name', 'N/A')} ({pay_period})"
        
        if "One-Time" in pkg_details.get('name', ''):
             desc = f"One-Time Charge ({pay_period})"
        
        line_item_data = [
            [Paragraph(desc, styles['Left']), Paragraph(f"{invoice_data['amount']:.2f}", styles['Right'])]
        ]
        line_item_table = Table(line_item_data, colWidths=['70%', '30%'])
        line_item_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ]))
        elements.append(line_item_table)

        elements.append(Spacer(1, 2 * mm))
        elements.append(Paragraph("--------------------------------------------------", styles['Center']))
        elements.append(Spacer(1, 2 * mm))
        
        elements.append(Paragraph(f"Total Paid: {invoice_data['amount']:.2f} BDT", styles['Total']))
        
        elements.append(Spacer(1, 5 * mm))
        elements.append(Paragraph("Thank you for your payment!", styles['Center']))
        elements.append(Paragraph(f"Powered by HUDA ISP SOLUTIONS", styles['Center']))

        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return True, pdf_bytes
    except Exception as e:
        print(f"Error generating thermal PDF: {e}")
        return False, str(e)