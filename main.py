import io
import os
import re
import zipfile
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, field

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import openpyxl
from lxml import etree
import pdfplumber

app = FastAPI(title="ASYCUDA XML Generator - Safe Cargo Services")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Data classes for invoice parsing ───────────────────────────────────────────

@dataclass
class InvoiceItem:
    description: str
    quantity: int
    total_value: float  # per-item total (qty * unit_price)


@dataclass
class InvoiceData:
    tracking_number: str
    items: List[InvoiceItem] = field(default_factory=list)
    grand_total: float = 0.0
    invoice_date: str = ""
    buyer_name: str = ""


# ─── PDF Invoice Parsing ────────────────────────────────────────────────────────

def extract_tracking_from_filename(filename: str) -> str:
    """Extract tracking number from an invoice PDF filename."""
    name = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
    mlbs_match = re.search(r'(MLBS\d+[A-Za-z]*)', name, re.IGNORECASE)
    if mlbs_match:
        return mlbs_match.group(1).upper()
    tracking_patterns = [
        r'([A-Z]{2,4}\d{6,}[A-Za-z0-9]*)',
    ]
    for pattern in tracking_patterns:
        match = re.search(pattern, name)
        if match:
            return match.group(1)
    parts = name.split('_')
    if parts:
        return parts[-1].strip()
    return name


def parse_invoice_pdf(file_bytes: bytes, filename: str) -> Optional[InvoiceData]:
    """Parse an invoice PDF and extract item data."""
    tracking = extract_tracking_from_filename(filename)
    invoice = InvoiceData(tracking_number=tracking)
    
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            all_text = ""
            all_tables = []
            
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                all_text += page_text + "\n"
                tables = page.extract_tables()
                if tables:
                    all_tables.extend(tables)
            
            print(f"  [PDF DEBUG] {filename}: {len(all_tables)} tables, {len(all_text)} chars text")
            
            if all_tables:
                invoice = _parse_from_tables(all_tables, all_text, tracking)
                if invoice.items:
                    print(f"  [PDF DEBUG] Strategy 1 (tables) found {len(invoice.items)} items")
            
            if not invoice.items:
                invoice = _parse_from_text(all_text, tracking)
                if invoice.items:
                    print(f"  [PDF DEBUG] Strategy 2 (text regex) found {len(invoice.items)} items")
            
            if not invoice.items:
                invoice = _parse_from_dollar_lines(all_text, tracking)
                if invoice.items:
                    print(f"  [PDF DEBUG] Strategy 3 (dollar scan) found {len(invoice.items)} items")
            
            if not invoice.items:
                print(f"  [PDF DEBUG] ALL STRATEGIES FAILED for {filename}")
                print(f"  [PDF DEBUG] First 500 chars of text: {repr(all_text[:500])}")
            
            date_match = re.search(
                r'(?:Invoice\s*Date|Date)[:\s]*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+\w+\s+\d{4})',
                all_text, re.IGNORECASE
            )
            if date_match:
                invoice.invoice_date = date_match.group(1).strip()
            
            total_match = re.search(
                r'(?:Grand\s*Total|Total\s*Amount|Total\s*Due|Total)[:\s]*\$?\s*([\d,]+\.?\d*)',
                all_text, re.IGNORECASE
            )
            if total_match:
                try:
                    invoice.grand_total = float(total_match.group(1).replace(',', ''))
                except ValueError:
                    pass
            
            if invoice.grand_total == 0 and invoice.items:
                invoice.grand_total = sum(item.total_value for item in invoice.items)
            
            buyer_match = re.search(
                r'(?:Ship\s*To|Buyer|Consignee|Customer|Deliver\s*to|Bill\s*To|Recipient)[:\s]*([A-Za-z][A-Za-z\s.]+)',
                all_text, re.IGNORECASE
            )
            if buyer_match:
                invoice.buyer_name = buyer_match.group(1).strip()
    
    except Exception as e:
        print(f"  [PDF ERROR] Failed to parse PDF {filename}: {type(e).__name__}: {e}")
    
    return invoice if invoice.items else None


def _parse_from_tables(tables: list, full_text: str, tracking: str) -> InvoiceData:
    """Parse invoice items from extracted tables."""
    invoice = InvoiceData(tracking_number=tracking)
    
    for table in tables:
        if not table or len(table) < 2:
            continue
        
        header_row = None
        desc_col = qty_col = price_col = total_col = None
        
        for row_idx, row in enumerate(table):
            if not row:
                continue
            row_lower = [str(cell).lower().strip() if cell else "" for cell in row]
            
            has_desc = any(h in cell for cell in row_lower 
                         for h in ['description', 'item', 'product', 'goods', 'name', 'particulars'])
            has_qty = any(h in cell for cell in row_lower 
                        for h in ['qty', 'quantity', 'pcs', 'units', 'pieces'])
            has_price = any(h in cell for cell in row_lower 
                          for h in ['price', 'unit', 'rate', 'cost', 'each'])
            has_total = any(h in cell for cell in row_lower 
                          for h in ['total', 'amount', 'value', 'subtotal', 'ext'])
            
            if has_desc and (has_qty or has_price or has_total):
                header_row = row_idx
                for ci, cell in enumerate(row_lower):
                    if any(h in cell for h in ['description', 'item', 'product', 'goods', 'name', 'particulars']):
                        if desc_col is None:
                            desc_col = ci
                    elif any(h in cell for h in ['qty', 'quantity', 'pcs', 'units', 'pieces']):
                        qty_col = ci
                    elif any(h in cell for h in ['total', 'amount', 'subtotal', 'ext']) and 'unit' not in cell and 'grand' not in cell:
                        total_col = ci
                    elif any(h in cell for h in ['price', 'unit', 'rate', 'cost', 'each']):
                        price_col = ci
                break
        
        if header_row is None or desc_col is None:
            if len(table) >= 3 and len(table[0]) >= 3:
                header_row = 0
                if len(table[0]) >= 5:
                    desc_col = 1
                    qty_col = 2
                    price_col = 3
                    total_col = 4
                elif len(table[0]) >= 4:
                    desc_col = 1
                    qty_col = 2
                    total_col = 3
                elif len(table[0]) >= 3:
                    desc_col = 0
                    qty_col = 1
                    total_col = 2
                else:
                    continue
                print(f"  [PDF DEBUG] Table header not identified by keywords; using positional guess: desc={desc_col}, qty={qty_col}, total={total_col}")
            else:
                continue
        
        for row in table[header_row + 1:]:
            if not row or len(row) <= desc_col:
                continue
            
            desc = str(row[desc_col]).strip() if row[desc_col] else ""
            if not desc or desc.lower() in ('', 'total', 'grand total', 'subtotal', 'none', 'null'):
                continue
            if re.match(r'^[\d.,\s$]+$', desc):
                continue
            
            qty = 1
            if qty_col is not None and qty_col < len(row) and row[qty_col]:
                try:
                    qty_str = re.sub(r'[^\d.]', '', str(row[qty_col]))
                    qty = int(float(qty_str)) if qty_str else 1
                except (ValueError, TypeError):
                    qty = 1
            
            total_val = 0.0
            if total_col is not None and total_col < len(row) and row[total_col]:
                try:
                    val_str = re.sub(r'[^\d.]', '', str(row[total_col]))
                    total_val = float(val_str) if val_str else 0.0
                except (ValueError, TypeError):
                    total_val = 0.0
            
            if total_val == 0.0 and price_col is not None and price_col < len(row) and row[price_col]:
                try:
                    val_str = re.sub(r'[^\d.]', '', str(row[price_col]))
                    unit_price = float(val_str) if val_str else 0.0
                    total_val = unit_price * qty
                except (ValueError, TypeError):
                    pass
            
            if desc and total_val > 0:
                invoice.items.append(InvoiceItem(
                    description=desc,
                    quantity=qty,
                    total_value=round(total_val, 2)
                ))
    
    return invoice


def _parse_from_text(text: str, tracking: str) -> InvoiceData:
    """Parse invoice items from raw text using regex patterns."""
    invoice = InvoiceData(tracking_number=tracking)
    
    patterns = [
        (4, r'^\s*\d+[\s.)]+(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$'),
        (4, r'^(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$'),
        (3, r'^\s*\d+[\s.)]+(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s*$'),
        (3, r'^(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s*$'),
        (2, r'^\s*\d+[\s.)]+(.+?)\s+\$?([\d,]+\.\d{2})\s*$'),
        (3, r"([A-Za-z][A-Za-z\s']+?)\s*\((\d+)\s*,\s*\$?([\d,]+\.\d{2})\)"),
    ]
    
    skip_keywords = ['subtotal', 'shipping', 'tax', 'bill to', 'ship to', 'page',
                     'description', 'qty', 'quantity', 'unit price', 'amount',
                     'invoice', 'date', 'customer', 'tracking', 'proforma',
                     'commercial', 'from:', 'to:', 'address', 'phone', 'email',
                     'payment', 'terms', 'notes', 'thank you']
    
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        line_lower = line.lower()
        
        if any(skip in line_lower for skip in skip_keywords):
            if not re.search(r'\$?\d+\.\d{2}', line):
                continue
            if any(line_lower.startswith(skip) for skip in skip_keywords):
                continue
        
        if re.match(r'^\s*(grand\s*)?total[:\s]*\$?[\d,.]+\s*$', line, re.IGNORECASE):
            continue
        if re.match(r'^\s*\$?[\d,. ]+$', line):
            continue
        
        for ncols, pattern in patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                groups = match.groups()
                desc = groups[0].strip()
                
                if not desc or len(desc) < 2 or not re.match(r'[A-Za-z]', desc):
                    continue
                if desc.lower() in ('item', 'product', 'goods', 'name', 'no', 'total', 'grand total'):
                    continue
                
                if ncols == 4:
                    qty = int(groups[1])
                    total_val = float(groups[3].replace(',', ''))
                elif ncols == 3:
                    qty = int(groups[1])
                    unit_price = float(groups[2].replace(',', ''))
                    total_val = unit_price * qty
                elif ncols == 2:
                    qty = 1
                    total_val = float(groups[1].replace(',', ''))
                else:
                    continue
                
                if total_val > 0:
                    invoice.items.append(InvoiceItem(
                        description=desc,
                        quantity=qty,
                        total_value=round(total_val, 2)
                    ))
                break
    
    return invoice


def _parse_from_dollar_lines(text: str, tracking: str) -> InvoiceData:
    """Fallback: scan for lines containing text + dollar amounts."""
    invoice = InvoiceData(tracking_number=tracking)
    
    skip_starts = ['total', 'grand', 'subtotal', 'shipping', 'tax', 'invoice',
                   'date', 'customer', 'bill', 'ship', 'from', 'to', 'tracking',
                   'payment', 'commercial', 'proforma', 'page', 'note']
    
    lines = text.split('\n')
    found_items = []
    
    for line in lines:
        line = line.strip()
        if not line or len(line) < 5:
            continue
        
        line_lower = line.lower()
        
        if any(line_lower.startswith(s) for s in skip_starts):
            continue
        if 'total' in line_lower and not re.search(r'[a-zA-Z]{3,}.*\d', line_lower.replace('total', '')):
            continue
        
        match = re.match(
            r'^\s*\d*[\s.)]*([A-Za-z][A-Za-z\s\'\-&,./]+?)\s+(?:\d+\s+)?(?:\$?[\d,]+\.\d{2}\s+)*\$?([\d,]+\.\d{2})\s*$',
            line
        )
        if match:
            desc = match.group(1).strip()
            last_amount = float(match.group(2).replace(',', ''))
            
            if len(desc) >= 2 and last_amount > 0 and last_amount < 10000:
                found_items.append((desc, 1, last_amount))
    
    if len(found_items) >= 2:
        for desc, qty, total in found_items:
            invoice.items.append(InvoiceItem(
                description=desc,
                quantity=qty,
                total_value=round(total, 2)
            ))
    
    return invoice


def parse_all_invoices(pdf_files_data: List[tuple]) -> dict:
    """Parse all uploaded PDFs and return a dict keyed by tracking number."""
    invoices = {}
    
    for filename, file_bytes in pdf_files_data:
        print(f"  [INVOICE] Parsing: {filename} ({len(file_bytes)} bytes)")
        invoice = parse_invoice_pdf(file_bytes, filename)
        if invoice and invoice.items:
            invoices[invoice.tracking_number] = invoice
            print(f"  [INVOICE] SUCCESS: {filename} -> {invoice.tracking_number} ({len(invoice.items)} items, ${invoice.grand_total:.2f})")
            for i, item in enumerate(invoice.items):
                print(f"    Item {i+1}: {item.description} | qty={item.quantity} | ${item.total_value:.2f}")
        else:
            tracking = extract_tracking_from_filename(filename)
            print(f"  [INVOICE] FAILED: No items from {filename} (tracking: {tracking})")
    
    return invoices


def match_invoice_to_row(row_tracking: str, invoices: dict) -> Optional[InvoiceData]:
    """Match an invoice to a spreadsheet row by tracking number."""
    if not invoices:
        return None
    
    row_tracking_clean = safe_str(row_tracking).strip().upper()
    
    if not row_tracking_clean:
        return None
    
    for inv_tracking, invoice in invoices.items():
        if inv_tracking.upper() == row_tracking_clean:
            print(f"    [MATCH] Exact match: {row_tracking_clean} == {inv_tracking}")
            return invoice
    
    for inv_tracking, invoice in invoices.items():
        inv_upper = inv_tracking.upper()
        if row_tracking_clean in inv_upper or inv_upper in row_tracking_clean:
            print(f"    [MATCH] Partial match: {row_tracking_clean} <-> {inv_tracking}")
            return invoice
    
    row_digits = re.sub(r'[^0-9]', '', row_tracking_clean)
    if row_digits:
        for inv_tracking, invoice in invoices.items():
            inv_digits = re.sub(r'[^0-9]', '', inv_tracking)
            if inv_digits and (row_digits in inv_digits or inv_digits in row_digits):
                print(f"    [MATCH] Digit match: {row_tracking_clean} ({row_digits}) <-> {inv_tracking} ({inv_digits})")
                return invoice
    
    print(f"    [MATCH] No match found for: {row_tracking_clean}")
    print(f"    [MATCH] Available invoices: {list(invoices.keys())}")
    return None



def parse_bracketed_array(value: str) -> list:
    """Parse bracketed arrays like [item1][item2][item3] into a list.
    Each [bracket] is treated as ONE item — commas inside brackets are NOT separators.
    """
    if not value or str(value).strip() == "":
        return []
    value = str(value).strip()
    # Extract each [bracket] as a whole item — never split on commas inside brackets
    matches = re.findall(r'\[([^\]]*)\]', value)
    if matches:
        return [m.strip() for m in matches if m.strip()]
    # No brackets at all — treat whole string as one item
    return [value.strip()] if value.strip() else []


def safe_str(val) -> str:
    """Convert value to string, handling None."""
    if val is None:
        return ""
    return str(val).strip()


def create_null_element(parent, tag):
    """Create an element with <null/> child."""
    elem = etree.SubElement(parent, tag)
    etree.SubElement(elem, "null")
    return elem


def set_element_text(parent, tag, text=None, use_null=False):
    """Create a sub-element with text or null."""
    elem = etree.SubElement(parent, tag)
    if use_null:
        etree.SubElement(elem, "null")
    elif text is not None:
        elem.text = str(text)
    return elem


# ─── XML Generation ────────────────────────────────────────────────────────────


def fix_xml_output(xml_bytes: bytes) -> bytes:
    """Fix XML declaration to use double quotes and 4-space indentation to match ASYCUDA World format."""
    text = xml_bytes.decode('utf-8')
    text = text.replace("<?xml version='1.0' encoding='UTF-8' standalone='no'?>",
                        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
    lines = text.split('\n')
    fixed_lines = []
    for line in lines:
        stripped = line.lstrip(' ')
        spaces = len(line) - len(stripped)
        fixed_lines.append(' ' * (spaces * 2) + stripped)
    return '\n'.join(fixed_lines).encode('utf-8')

def generate_waybill_xml(row_data: dict, shipment_info: dict, row_index: int) -> bytes:
    """Generate House Waybill XML matching the exact Anguilla ASYCUDA World template."""
    
    voyage = shipment_info.get("voyage_number", "")
    master_awb = shipment_info["master_awb"]
    
    raw_date = shipment_info.get("date_of_departure", "")
    try:
        from datetime import datetime
        dt = datetime.strptime(raw_date, "%Y-%m-%d")
        formatted_date = f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
    except Exception:
        formatted_date = raw_date
    
    prev_doc_ref = f"{voyage}-{row_index}"
    
    items_desc = parse_bracketed_array(safe_str(row_data.get("items_description", "")))
    items_count = len(items_desc) if items_desc else 1
    goods_desc_joined = ", ".join(items_desc) if items_desc else safe_str(row_data.get("items_description", ""))
    hs_codes = parse_bracketed_array(safe_str(row_data.get("items_hs_codes", "")))
    first_hs = hs_codes[0] if hs_codes else ""
    
    shipper_addr = ", ".join(filter(None, [
        safe_str(row_data.get("shipper_address1", "")),
        safe_str(row_data.get("shipper_city", "")),
        f"{safe_str(row_data.get('shipper_state',''))} {safe_str(row_data.get('shipper_zip',''))}".strip(),
        safe_str(row_data.get("shipper_country", ""))
    ]))
    buyer_addr = "\n".join(filter(None, [
        safe_str(row_data.get("buyer_address1", "")),
        safe_str(row_data.get("buyer_city", "")),
        safe_str(row_data.get("buyer_state", "")),
        safe_str(row_data.get("buyer_phone", ""))
    ]))
    
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        '<AsycudaWorld_Manifest>',
        '<Identification_segment>',
        f'<Voyage_number>{voyage}</Voyage_number>',
        f'<Date_of_departure>{formatted_date}</Date_of_departure>',
        f'<Bol_reference>{safe_str(row_data.get("tracking_number", "")).upper()}</Bol_reference>',
        '<Customs_office_segment>',
        '<Code>00RB</Code>',
        '<Name>ROAD BAY</Name>',
        '</Customs_office_segment>',
        '</Identification_segment>',
        '<Bol_specific_segment>',
        f'<Line_number>{row_index}</Line_number>',
        f'<Sub_line_number>{row_index}</Sub_line_number>',
        '<Status>HSE</Status>',
        f'<Previous_document_reference>{prev_doc_ref}</Previous_document_reference>',
        '<Bol_Nature>23</Bol_Nature>',
        '<Unique_carrier_reference>',
        '<null/>',
        '</Unique_carrier_reference>',
        '<Fas/>',
        '<Total_number_of_containers>0</Total_number_of_containers>',
        '<Total_number_of_vehicles>0</Total_number_of_vehicles>',
        f'<Total_gross_mass_manifested>{safe_str(row_data.get("weight","0"))}</Total_gross_mass_manifested>',
        f'<Volume_in_cubic_meters>{safe_str(row_data.get("volume","0.2"))}</Volume_in_cubic_meters>',
        '<Number_of_sub_bols/>',
        '<Bol_type_segment>',
        '<Code>710</Code>',
        '<Name>Bill of lading</Name>',
        '</Bol_type_segment>',
        '<Exporter_segment>',
        '<Code>',
        '<null/>',
        '</Code>',
        f'<Name>{safe_str(row_data.get("shipper",""))}</Name>',
        f'<Address>{shipper_addr}</Address>',
        '</Exporter_segment>',
        '<Consignee_segment>',
        '<Code>',
        '<null/>',
        '</Code>',
        f'<Name>{safe_str(row_data.get("buyer_name",""))}</Name>',
        f'<Address>{buyer_addr}</Address>',
        '</Consignee_segment>',
        '<Notify_segment>',
        '<Code>',
        '<null/>',
        '</Code>',
        '<Name>Null</Name>',
        '<Address>Null</Address>',
        '</Notify_segment>',
        '<Place_of_loading_segment>',
        f'<Code>{shipment_info.get("port_of_loading_code","USMIA")}</Code>',
        f'<Name>{shipment_info.get("port_of_loading_name","MIAMI, USA")}</Name>',
        '</Place_of_loading_segment>',
        '<Place_of_unloading_segment>',
        '<Code>AIAXA</Code>',
        '<Name>ANGUILLA</Name>',
        '</Place_of_unloading_segment>',
        '<Packages_segment>',
        '<Package_type_code>PK</Package_type_code>',
        '<Package_type_name>Package</Package_type_name>',
        f'<Number_of_packages>{items_count}</Number_of_packages>',
        '</Packages_segment>',
        '<Shipping_segment>',
        f'<Shipping_marks>{safe_str(row_data.get("tracking_number",""))}</Shipping_marks>',
        '</Shipping_segment>',
        '<Goods_segment>',
        f'<Goods_description>{goods_desc_joined}</Goods_description>',
        '<Place_of_origin>',
        '<null/>',
        '</Place_of_origin>',
        '<Place_of_destination>',
        '<null/>',
        '</Place_of_destination>',
        f'<Goods_hs_code>{first_hs}</Goods_hs_code>',
        '</Goods_segment>',
        '<Freight_segment>',
        '<Value/>',
        '<Currency>',
        '<null/>',
        '</Currency>',
        '<Indicator_segment>',
        '<Code>',
        '<null/>',
        '</Code>',
        '<Name>',
        '<null/>',
        '</Name>',
        '</Indicator_segment>',
        '</Freight_segment>',
        '<Customs_segment>',
        '<Value/>',
        '<Currency>',
        '<null/>',
        '</Currency>',
        '</Customs_segment>',
        '<Transport_segment>',
        '<Value/>',
        '<Currency>',
        '<null/>',
        '</Currency>',
        '<Vessel_loading_code>',
        '<null/>',
        '</Vessel_loading_code>',
        '<Vessel_loading_name>',
        '<null/>',
        '</Vessel_loading_name>',
        '<Vessel_discharge_code>',
        '<null/>',
        '</Vessel_discharge_code>',
        '<Vessel_discharge_name>',
        '<null/>',
        '</Vessel_discharge_name>',
        '</Transport_segment>',
        '<Insurance_segment>',
        '<Value/>',
        '<Currency>',
        '<null/>',
        '</Currency>',
        '</Insurance_segment>',
        '<Seals_segment>',
        '<Number_of_seals/>',
        '<Marks_of_seals>',
        '<null/>',
        '</Marks_of_seals>',
        '<Sealing_party_code>',
        '<null/>',
        '</Sealing_party_code>',
        '<Sealing_party_name>',
        '<null/>',
        '</Sealing_party_name>',
        '</Seals_segment>',
        '<Information_segment>',
        '<Information_part_a>',
        '<null/>',
        '</Information_part_a>',
        '</Information_segment>',
        '<Operations_segment>',
        '<Packages_remaining/>',
        '<Gross_mass_remaining/>',
        '<Location_segment>',
        '<Code>KOWH</Code>',
        '<Name>KING OCEAN WAREHOUSE</Name>',
        '<Information>KING OCEAN WAREHOUSE</Information>',
        '</Location_segment>',
        '<Onward_transport_segment>',
        '<Transit_segment>',
        '<Customs_office_code>',
        '<null/>',
        '</Customs_office_code>',
        '<Customs_office_name>',
        '<null/>',
        '</Customs_office_name>',
        '<Document_reference>',
        '<null/>',
        '</Document_reference>',
        '</Transit_segment>',
        '<Transhipment_segment>',
        '<Transipment_location_code>',
        '<null/>',
        '</Transipment_location_code>',
        '<Transhipment_location_name>',
        '<null/>',
        '</Transhipment_location_name>',
        '<Document_reference>',
        '<null/>',
        '</Document_reference>',
        '</Transhipment_segment>',
        '<Onward_carrier_segment>',
        '<Code>',
        '<null/>',
        '</Code>',
        '<Name>',
        '<null/>',
        '</Name>',
        '</Onward_carrier_segment>',
        '</Onward_transport_segment>',
        '</Operations_segment>',
        '</Bol_specific_segment>',
        '</AsycudaWorld_Manifest>',
    ]
    
    xml_str = '\n'.join(xml_lines)
    import re as _re
    def _upper_text(m):
        tag_open, text, tag_close = m.group(1), m.group(2), m.group(3)
        return tag_open + text.upper() + tag_close
    xml_str = _re.sub(r'(>)([^<>]+)(<)', _upper_text, xml_str)
    return xml_str.encode('utf-8')




def generate_declaration_xml(row_data: dict, shipment_info: dict, row_index: int,
                             invoice: Optional[InvoiceData] = None) -> bytes:
    """Generate Customs Declaration XML (ASYCUDA SAD) for a single row."""
    current_year = str(datetime.now().year)
    XCD_RATE = 2.6882
    
    tracking = safe_str(row_data.get("tracking_number", "")).strip().upper()
    buyer_name = safe_str(row_data.get("buyer_name", "")).strip().upper()
    shipper = safe_str(row_data.get("shipper", "")).strip().upper()
    shipper_country = safe_str(row_data.get("shipper_country", "US")).strip().upper()
    ddp_ddu = safe_str(row_data.get("ddp_ddu", "")).strip().upper()
    if not ddp_ddu:
        ddp_ddu = "FOB"
    
    weight = float(safe_str(row_data.get("weight", "0")) or "0")
    fob_usd = float(safe_str(row_data.get("fob_verified", "0")) or "0")
    cif_usd = float(safe_str(row_data.get("cif_verified", "0")) or "0")
    
    fob_xcd = round(fob_usd * XCD_RATE, 2)
    cif_xcd = round(cif_usd * XCD_RATE, 2)
    other_cost_usd = round(cif_usd - fob_usd, 2)
    other_cost_xcd = round(other_cost_usd * XCD_RATE, 2)
    
    master_awb = shipment_info.get("master_awb", "")
    carrier_name = shipment_info.get("carrier_name", "").upper()
    
    raw_date = shipment_info.get("date_of_departure", "")
    try:
        dt = datetime.strptime(raw_date, "%Y-%m-%d")
        formatted_date = f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
    except Exception:
        formatted_date = raw_date
    
    items_hs = parse_bracketed_array(safe_str(row_data.get("items_hs_codes", "")))
    items_desc_raw = parse_bracketed_array(safe_str(row_data.get("items_description", "")))
    items_qty_raw = parse_bracketed_array(safe_str(row_data.get("items_quantities", "")))
    
    if invoice and invoice.items:
        item_count = len(invoice.items)
        use_invoice = True
    else:
        item_count = max(len(items_desc_raw), 1)
        use_invoice = False
    
    item_values = []
    for i in range(item_count):
        if use_invoice:
            item_fob_usd = invoice.items[i].total_value
        else:
            item_fob_usd = fob_usd / item_count if item_count > 0 else 0
        
        if fob_usd > 0:
            item_cif_usd = item_fob_usd * (cif_usd / fob_usd)
        else:
            item_cif_usd = item_fob_usd
        
        item_other_cost_usd = item_cif_usd - item_fob_usd
        item_fob_xcd = round(item_fob_usd * XCD_RATE, 2)
        item_cif_xcd = round(item_cif_usd * XCD_RATE, 2)
        item_other_cost_xcd = round(item_other_cost_usd * XCD_RATE, 2)
        alpha = round(item_fob_usd / fob_usd, 6) if fob_usd > 0 else round(1.0 / item_count, 6)
        item_weight = round(weight / item_count, 2) if item_count > 0 else 0
        
        item_values.append({
            "fob_usd": round(item_fob_usd, 2),
            "fob_xcd": item_fob_xcd,
            "cif_usd": round(item_cif_usd, 2),
            "cif_xcd": item_cif_xcd,
            "other_cost_usd": round(item_other_cost_usd, 2),
            "other_cost_xcd": item_other_cost_xcd,
            "alpha": alpha,
            "weight": item_weight,
        })
    
    x = []
    x.append('<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
    x.append(f'<ASYCUDA id="{1001651 + row_index}">')
    
    x.append('<Export_release>')
    x.append('<Date_of_exit/>')
    x.append('<Time_of_exit/>')
    x.append('<Actual_office_of_exit_code><null/></Actual_office_of_exit_code>')
    x.append('<Actual_office_of_exit_name><null/></Actual_office_of_exit_name>')
    x.append('<Exit_reference><null/></Exit_reference>')
    x.append('<Comments><null/></Comments>')
    x.append('</Export_release>')
    
    x.append('<Assessment_notice>')
    x.append(f'<Registration_year>{current_year}</Registration_year>')
    x.append(f'<Assessment_year>{current_year}</Assessment_year>')
    x.append('<Total_item_taxes/>')
    x.append('<Statement_number><null/></Statement_number>')
    x.append('<Statement_date/>')
    x.append('<Statement_serial><null/></Statement_serial>')
    x.append('<Items_taxes/>')
    x.append('<Global_taxes/>')
    tax_codes = ["ICD", "CSF", "GDT"]
    for i in range(14):
        if i < 3:
            x.append(f'<Item_tax><Tax_code>{tax_codes[i]}</Tax_code><Tax_amount/><Tax_MP><null/></Tax_MP></Item_tax>')
        else:
            x.append('<Item_tax><Tax_code><null/></Tax_code><Tax_amount/><Tax_MP><null/></Tax_MP></Item_tax>')
    x.append('</Assessment_notice>')
    
    x.append('<Property>')
    x.append('<Sad_flow>I</Sad_flow>')
    x.append('<Forms><Number_of_the_form>1</Number_of_the_form><Total_number_of_forms>1</Total_number_of_forms></Forms>')
    x.append('<Nbers>')
    x.append('<Number_of_loading_lists/>')
    x.append(f'<Total_number_of_items>{item_count}</Total_number_of_items>')
    x.append('<Total_number_of_packages>1</Total_number_of_packages>')
    x.append('</Nbers>')
    x.append('<Place_of_declaration><null/></Place_of_declaration>')
    x.append('<Date_of_declaration/>')
    x.append('<Selected_page>1</Selected_page>')
    x.append('</Property>')
    
    x.append('<Identification>')
    x.append('<Office_segment>')
    x.append('<Customs_clearance_office_code>00RB</Customs_clearance_office_code>')
    x.append('<Customs_Clearance_office_name>ROAD BAY</Customs_Clearance_office_name>')
    x.append('</Office_segment>')
    x.append('<Type>')
    x.append('<Type_of_declaration>IM</Type_of_declaration>')
    x.append('<Declaration_gen_procedure_code>4</Declaration_gen_procedure_code>')
    x.append('<Type_of_transit_document><null/></Type_of_transit_document>')
    x.append('</Type>')
    x.append(f'<Manifest_reference_number>{shipment_info.get("manifest_reference", "")}</Manifest_reference_number>')
    x.append('<Registration><Serial_number><null/></Serial_number><Number/><Date/></Registration>')
    x.append('<Assessment><Serial_number><null/></Serial_number><Number/><Date/></Assessment>')
    x.append('<receipt><Serial_number><null/></Serial_number><Number/><Date/></receipt>')
    x.append('</Identification>')
    
    x.append('<Traders>')
    x.append('<Exporter>')
    x.append('<Exporter_code><null/></Exporter_code>')
    x.append(f'<Exporter_name>{shipper}\\n{shipper_country}</Exporter_name>')
    x.append('</Exporter>')
    x.append('<Consignee>')
    x.append('<Consignee_code>999</Consignee_code>')
    x.append('<Consignee_name>Self\\nThe Valley\\nAnguilla</Consignee_name>')
    x.append('</Consignee>')
    x.append('<Financial>')
    x.append('<Financial_code><null/></Financial_code>')
    x.append(f'<Financial_name>{buyer_name}</Financial_name>')
    x.append('</Financial>')
    x.append('</Traders>')
    
    x.append('<Declarant>')
    x.append('<Declarant_code>DC2114</Declarant_code>')
    x.append('<Declarant_name>Safe Cargo Services\\nSandy Ground\\nAnguilla</Declarant_name>')
    x.append('<Declarant_representative>Evette Harrigan</Declarant_representative>')
    x.append('<Reference>')
    x.append(f'<Year>{current_year}</Year>')
    x.append(f'<Number>{row_index}/{buyer_name}/{tracking}</Number>')
    x.append('</Reference>')
    x.append('</Declarant>')
    
    x.append('<General_information>')
    x.append('<Country>')
    x.append('<Country_first_destination>US</Country_first_destination>')
    x.append('<Trading_country>US</Trading_country>')
    x.append('<Export>')
    x.append('<Export_country_code>US</Export_country_code>')
    x.append('<Export_country_name>United States</Export_country_name>')
    x.append('<Export_country_region/>')
    x.append('</Export>')
    x.append('<Destination>')
    x.append('<Destination_country_code>AI</Destination_country_code>')
    x.append('<Destination_country_name>Anguilla</Destination_country_name>')
    x.append('<Destination_country_region/>')
    x.append('</Destination>')
    # Country name lookup for origin
    country_names = {"US": "United States", "CN": "China", "GB": "United Kingdom", "CA": "Canada", "JP": "Japan", "DE": "Germany", "FR": "France", "IN": "India", "KR": "Korea, Republic of", "TW": "Taiwan", "MX": "Mexico", "BR": "Brazil", "IT": "Italy"}
    origin_country_name = country_names.get(shipper_country, shipper_country)
    x.append(f'<Country_of_origin_name>{origin_country_name}</Country_of_origin_name>')
    x.append('</Country>')
    x.append(f'<Value_details>{cif_usd}</Value_details>')
    x.append('<CAP/>')
    x.append('<Additional_information><null/></Additional_information>')
    x.append('<Comments_free_text/>')
    x.append('</General_information>')
    
    x.append('<Transport>')
    x.append('<Means_of_transport>')
    x.append('<Departure_arrival_information>')
    x.append(f'<Identity>{carrier_name}</Identity>')
    x.append('<Nationality>KN</Nationality>')
    x.append('</Departure_arrival_information>')
    x.append('<Border_information>')
    x.append(f'<Identity>{carrier_name}</Identity>')
    x.append('<Nationality>KN</Nationality>')
    x.append('<Mode>1</Mode>')
    x.append('</Border_information>')
    x.append('<Inland_mode_of_transport><null/></Inland_mode_of_transport>')
    x.append('</Means_of_transport>')
    x.append('<Container_flag>false</Container_flag>')
    x.append('<Delivery_terms>')
    x.append(f'<Code>{ddp_ddu}</Code>')
    x.append('<Place>Anguilla</Place>')
    x.append('<Situation/>')
    x.append('</Delivery_terms>')
    x.append('<Border_office>')
    x.append('<Code>00RB</Code>')
    x.append('<Name>ROAD BAY</Name>')
    x.append('</Border_office>')
    x.append('<Place_of_loading>')
    x.append('<Code>AIAXA</Code>')
    x.append('<Name>ANGUILLA</Name>')
    x.append('<Country><null/></Country>')
    x.append('</Place_of_loading>')
    x.append('<Location_of_goods>KOWH</Location_of_goods>')
    x.append('</Transport>')
    
    x.append('<Financial>')
    x.append('<Financial_transaction><code1><null/></code1><code2><null/></code2></Financial_transaction>')
    x.append('<Bank><Code><null/></Code><Name><null/></Name><Branch><null/></Branch><Reference><null/></Reference></Bank>')
    x.append('<Terms><Code><null/></Code><Description><null/></Description></Terms>')
    x.append('<Total_invoice/>')
    x.append('<Deffered_payment_reference><null/></Deffered_payment_reference>')
    x.append('<Mode_of_payment>CASH</Mode_of_payment>')
    x.append('<Amounts><Total_manual_taxes/><Global_taxes>0</Global_taxes><Totals_taxes/></Amounts>')
    x.append('<Guarantee><Name><null/></Name><Amount>0</Amount><Date/><Excluded_country><Code><null/></Code><Name><null/></Name></Excluded_country></Guarantee>')
    x.append('</Financial>')
    
    x.append('<Warehouse>')
    x.append('<Identification><null/></Identification>')
    x.append('<Delay/>')
    x.append('</Warehouse>')
    
    x.append('<Transit>')
    x.append('<Principal><Code><null/></Code><Name><null/></Name><Representative><null/></Representative></Principal>')
    x.append('<Signature><Place><null/></Place><Date/></Signature>')
    x.append('<Destination><Office><null/></Office><Country><null/></Country></Destination>')
    x.append('<Seals><Number/><Identity><null/></Identity></Seals>')
    x.append('<Result_of_control><null/></Result_of_control>')
    x.append('<Time_limit/>')
    x.append('<Officer_name><null/></Officer_name>')
    x.append('</Transit>')
    
    x.append('<Valuation>')
    x.append('<Calculation_working_mode>0</Calculation_working_mode>')
    x.append(f'<Weight><Gross_weight>{weight}</Gross_weight></Weight>')
    x.append(f'<Total_cost>{other_cost_xcd}</Total_cost>')
    x.append(f'<Total_CIF>{cif_xcd}</Total_CIF>')
    x.append('<Gs_Invoice>')
    x.append(f'<Amount_national_currency>{fob_xcd}</Amount_national_currency>')
    x.append(f'<Amount_foreign_currency>{fob_usd}</Amount_foreign_currency>')
    x.append('<Currency_code>USD</Currency_code>')
    x.append('<Currency_name>No foreign currency</Currency_name>')
    x.append('<Currency_rate>2.6882</Currency_rate>')
    x.append('</Gs_Invoice>')
    x.append('<Gs_external_freight><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></Gs_external_freight>')
    x.append('<Gs_internal_freight><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></Gs_internal_freight>')
    x.append('<Gs_insurance><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></Gs_insurance>')
    x.append('<Gs_other_cost>')
    x.append(f'<Amount_national_currency>{other_cost_xcd}</Amount_national_currency>')
    x.append(f'<Amount_foreign_currency>{other_cost_usd}</Amount_foreign_currency>')
    x.append('<Currency_code>USD</Currency_code>')
    x.append('<Currency_name>No foreign currency</Currency_name>')
    x.append('<Currency_rate>2.6882</Currency_rate>')
    x.append('</Gs_other_cost>')
    x.append('<Gs_deduction><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></Gs_deduction>')
    x.append('<Total>')
    x.append(f'<Total_invoice>{fob_usd}</Total_invoice>')
    x.append(f'<Total_weight>{weight}</Total_weight>')
    x.append('</Total>')
    x.append('</Valuation>')
    
    for i in range(item_count):
        iv = item_values[i]
        
        if use_invoice:
            item_desc = invoice.items[i].description.upper()
            item_qty = invoice.items[i].quantity
        else:
            item_desc = items_desc_raw[i].upper() if i < len(items_desc_raw) else ""
            try:
                item_qty = int(items_qty_raw[i]) if i < len(items_qty_raw) else 1
            except (ValueError, TypeError):
                item_qty = 1
        
        hs_raw = items_hs[i] if i < len(items_hs) else ""
        hs_digits = re.sub(r'[^0-9]', '', hs_raw)
        if len(hs_digits) >= 10:
            commodity_code = hs_digits[:8]
            precision_1 = hs_digits[8:10]
        elif len(hs_digits) >= 8:
            commodity_code = hs_digits[:8]
            precision_1 = "00"
        else:
            commodity_code = hs_digits
            precision_1 = "00"
        
        x.append('<Item>')
        
        if i == 0:
            x.append('<Attached_documents>')
            x.append('<Attached_document_code>INV</Attached_document_code>')
            x.append('<Attached_document_name>Invoice</Attached_document_name>')
            x.append(f'<Attached_document_reference>{tracking}</Attached_document_reference>')
            x.append(f'<Attached_document_date>{formatted_date}</Attached_document_date>')
            x.append('</Attached_documents>')
            x.append('<Attached_documents>')
            x.append('<Attached_document_code>BOL</Attached_document_code>')
            x.append('<Attached_document_name>Bill of Lading or Air Waybill</Attached_document_name>')
            x.append(f'<Attached_document_reference>{tracking}</Attached_document_reference>')
            x.append(f'<Attached_document_date>{formatted_date}</Attached_document_date>')
            x.append('</Attached_documents>')
        
        pkg_count = 1 if i == 0 else 0
        x.append('<Packages>')
        x.append(f'<Number_of_packages>{pkg_count}</Number_of_packages>')
        x.append('<Marks1_of_packages>NA</Marks1_of_packages>')
        x.append('<Marks2_of_packages> </Marks2_of_packages>')
        x.append('<Kind_of_packages_code>BX</Kind_of_packages_code>')
        x.append('<Kind_of_packages_name>Box</Kind_of_packages_name>')
        x.append('</Packages>')
        
        x.append('<IncoTerms>')
        x.append(f'<Code>{ddp_ddu}</Code>')
        x.append('<Place>Anguilla</Place>')
        x.append('</IncoTerms>')
        
        x.append('<Tarification>')
        x.append('<Tarification_data><null/></Tarification_data>')
        x.append('<HScode>')
        x.append(f'<Commodity_code>{commodity_code}</Commodity_code>')
        x.append(f'<Precision_1>{precision_1}</Precision_1>')
        x.append('<Precision_2><null/></Precision_2>')
        x.append('<Precision_3><null/></Precision_3>')
        x.append('<Precision_4><null/></Precision_4>')
        x.append('</HScode>')
        x.append('<Preference_code><null/></Preference_code>')
        x.append('<Extended_customs_procedure>4000</Extended_customs_procedure>')
        x.append('<National_customs_procedure>000</National_customs_procedure>')
        x.append('<Quota><QuotaCode><null/></QuotaCode></Quota>')
        x.append('<Supplementary_unit>')
        x.append('<Supplementary_unit_rank>1</Supplementary_unit_rank>')
        x.append('<Suppplementary_unit_code>NMB</Suppplementary_unit_code>')
        x.append('<Suppplementary_unit_name>Number</Suppplementary_unit_name>')
        x.append(f'<Suppplementary_unit_quantity>{item_qty}</Suppplementary_unit_quantity>')
        x.append('</Supplementary_unit>')
        x.append('<Supplementary_unit>')
        x.append('<Supplementary_unit_rank>2</Supplementary_unit_rank>')
        x.append('<Suppplementary_unit_code><null/></Suppplementary_unit_code>')
        x.append('<Suppplementary_unit_name><null/></Suppplementary_unit_name>')
        x.append('<Suppplementary_unit_quantity/>')
        x.append('</Supplementary_unit>')
        x.append('<Supplementary_unit>')
        x.append('<Supplementary_unit_rank>3</Supplementary_unit_rank>')
        x.append('<Suppplementary_unit_code><null/></Suppplementary_unit_code>')
        x.append('<Suppplementary_unit_name><null/></Suppplementary_unit_name>')
        x.append('<Suppplementary_unit_quantity/>')
        x.append('</Supplementary_unit>')
        x.append('<Valuation_method_code><null/></Valuation_method_code>')
        x.append('<A.I._code><null/></A.I._code>')
        x.append('</Tarification>')
        
        x.append('<Goods_description>')
        x.append(f'<Country_of_origin_code>{shipper_country}</Country_of_origin_code>')
        x.append('<Country_of_origin_region><null/></Country_of_origin_region>')
        x.append('<Description_of_goods>- - Other</Description_of_goods>')
        x.append(f'<Commercial_Description>{item_desc}</Commercial_Description>')
        x.append('</Goods_description>')
        
        x.append('<Previous_doc>')
        x.append(f'<Summary_declaration>{tracking}</Summary_declaration>')
        x.append('<Summary_declaration_sl><null/></Summary_declaration_sl>')
        x.append('<Previous_document_reference><null/></Previous_document_reference>')
        x.append('<Previous_warehouse_code><null/></Previous_warehouse_code>')
        x.append('</Previous_doc>')
        
        x.append('<Licence_number><null/></Licence_number>')
        x.append('<Amount_deducted_from_licence/>')
        x.append('<Quantity_deducted_from_licence/>')
        x.append('<Free_text_1><null/></Free_text_1>')
        x.append('<Free_text_2><null/></Free_text_2>')
        
        x.append('<Taxation>')
        x.append('<Item_taxes_amount/>')
        x.append('<Item_taxes_guaranted_amount/>')
        x.append('<Item_taxes_mode_of_payment>1</Item_taxes_mode_of_payment>')
        x.append('<Counter_of_normal_mode_of_payment/>')
        x.append('<Displayed_item_taxes_amount/>')
        for _ in range(8):
            x.append('<Taxation_line><Duty_tax_code><null/></Duty_tax_code><Duty_tax_Base/><Duty_tax_rate/><Duty_tax_amount/><Duty_tax_MP><null/></Duty_tax_MP><Duty_tax_Type_of_calculation><null/></Duty_tax_Type_of_calculation></Taxation_line>')
        x.append('</Taxation>')
        
        x.append('<Valuation_item>')
        x.append('<Weight_itm>')
        x.append(f'<Gross_weight_itm>{iv["weight"]}</Gross_weight_itm>')
        x.append(f'<Net_weight_itm>{iv["weight"]}</Net_weight_itm>')
        x.append('</Weight_itm>')
        x.append(f'<Total_cost_itm>{iv["other_cost_xcd"]}</Total_cost_itm>')
        x.append(f'<Total_CIF_itm>{iv["cif_xcd"]}</Total_CIF_itm>')
        x.append('<Rate_of_adjustement>1</Rate_of_adjustement>')
        x.append(f'<Statistical_value>{iv["cif_xcd"]}</Statistical_value>')
        x.append(f'<Alpha_coeficient_of_apportionment>{iv["alpha"]}</Alpha_coeficient_of_apportionment>')
        x.append('<Item_Invoice>')
        x.append(f'<Amount_national_currency>{iv["fob_xcd"]}</Amount_national_currency>')
        x.append(f'<Amount_foreign_currency>{iv["fob_usd"]}</Amount_foreign_currency>')
        x.append('<Currency_code>USD</Currency_code>')
        x.append('<Currency_name>No foreign currency</Currency_name>')
        x.append('<Currency_rate>2.6882</Currency_rate>')
        x.append('</Item_Invoice>')
        x.append('<item_external_freight><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0.0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></item_external_freight>')
        x.append('<item_internal_freight><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0.0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></item_internal_freight>')
        x.append('<item_insurance><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0.0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></item_insurance>')
        x.append('<item_other_cost>')
        x.append(f'<Amount_national_currency>{iv["other_cost_xcd"]}</Amount_national_currency>')
        x.append(f'<Amount_foreign_currency>{iv["other_cost_usd"]}</Amount_foreign_currency>')
        x.append('<Currency_code>USD</Currency_code>')
        x.append('<Currency_name>No foreign currency</Currency_name>')
        x.append('<Currency_rate>2.6882</Currency_rate>')
        x.append('</item_other_cost>')
        x.append('<item_deduction><Amount_national_currency>0</Amount_national_currency><Amount_foreign_currency>0.0</Amount_foreign_currency><Currency_code><null/></Currency_code><Currency_name>No foreign currency</Currency_name><Currency_rate>0</Currency_rate></item_deduction>')
        x.append('<Market_valuer><Rate/><Currency_code><null/></Currency_code><Currency_amount/><Basis_description><null/></Basis_description><Basis_amount/></Market_valuer>')
        x.append('</Valuation_item>')
        
        x.append('</Item>')
    x.append('</ASYCUDA>')
    
    xml_str = '\n'.join(x)
    return xml_str.encode('utf-8')

# ─── Spreadsheet Parser ─────────────────────────────────────────────────────────

def parse_xlsx(file_bytes: bytes) -> list:
    """Parse the uploaded Excel file and return a list of row dictionaries."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
    ws = wb.active
    
    column_map = {
        "tracking number": "tracking_number",
        "cn35 awb": "cn35_awb",
        "ddp/ddu": "ddp_ddu",
        "weight": "weight",
        "cif verified": "cif_verified",
        "fob verified": "fob_verified",
        "items": "items",
        "items description": "items_description",
        "items quantities": "items_quantities",
        "items hs codes": "items_hs_codes",
        "buyer id": "buyer_id",
        "buyer name": "buyer_name",
        "buyer address1": "buyer_address1",
        "buyer city": "buyer_city",
        "buyer state": "buyer_state",
        "buyer phone": "buyer_phone",
        "buyer email": "buyer_email",
        "client": "client",
        "shipper": "shipper",
        "shipper address1": "shipper_address1",
        "shipper city": "shipper_city",
        "shipper state": "shipper_state",
        "shipper zip": "shipper_zip",
        "shipper country": "shipper_country",
        "shipper phone": "shipper_phone",
    }
    
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    
    header_row = rows[0]
    col_indices = {}
    for idx, cell in enumerate(header_row):
        if cell:
            normalized = str(cell).strip().lower()
            if normalized in column_map:
                col_indices[column_map[normalized]] = idx
    
    data_rows = []
    for row in rows[1:]:
        if not row or all(cell is None for cell in row):
            continue
        row_dict = {}
        for field_name, col_idx in col_indices.items():
            if col_idx < len(row):
                row_dict[field_name] = row[col_idx]
            else:
                row_dict[field_name] = None
        if not row_dict.get("tracking_number"):
            continue
        data_rows.append(row_dict)
    
    wb.close()
    return data_rows


# ─── API Endpoints ──────────────────────────────────────────────────────────────


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    expected_username = os.environ.get("SCS_MAILUSA_USERNAME", "")
    expected_password = os.environ.get("SCS_MAILUSA_PASSWORD", "")
    if username == expected_username and password == expected_password:
        return {"success": True, "token": "scs-mailusa-session"}
    return JSONResponse(
        status_code=401,
        content={"success": False, "message": "Invalid credentials"}
    )


@app.post("/generate")
async def generate_xmls(
    xlsx_file: UploadFile = File(...),
    master_awb: str = Form(...),
    voyage_number: str = Form(""),
    date_of_departure: str = Form(""),
    carrier_name: str = Form(""),
    manifest_reference: str = Form(""),
    pdf_files: List[UploadFile] = File(default=[]),
):
    """Generate ASYCUDA XML files (both waybills + declarations) from uploaded spreadsheet and optional invoice PDFs.
    Legacy combined endpoint - kept for backward compatibility."""
    
    if not master_awb or master_awb.strip() == "":
        raise HTTPException(status_code=400, detail="Master AWB / BOL number is required and cannot be empty.")
    
    xlsx_bytes = await xlsx_file.read()
    rows = parse_xlsx(xlsx_bytes)
    
    if not rows:
        raise HTTPException(status_code=400, detail="No valid data rows found in the spreadsheet.")
    
    shipment_info = {
        "master_awb": master_awb.strip(),
        "voyage_number": voyage_number.strip(),
        "date_of_departure": date_of_departure.strip(),
        "carrier_name": carrier_name.strip(),
        "manifest_reference": manifest_reference.strip(),
        "port_of_loading_code": "USMIA",
        "port_of_loading_name": "MIAMI, USA",
    }
    
    invoices = {}
    if pdf_files:
        pdf_data = []
        for pdf_file in pdf_files:
            if pdf_file.filename and pdf_file.filename.endswith('.pdf'):
                pdf_bytes = await pdf_file.read()
                if pdf_bytes:
                    pdf_data.append((pdf_file.filename, pdf_bytes))
        if pdf_data:
            print(f"Parsing {len(pdf_data)} invoice PDF(s)...")
            invoices = parse_all_invoices(pdf_data)
            print(f"Successfully parsed {len(invoices)} invoice(s) with item data")
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, row in enumerate(rows, start=1):
            tracking = safe_str(row.get("tracking_number", f"ROW_{idx}"))
            matched_invoice = match_invoice_to_row(tracking, invoices)
            if matched_invoice:
                print(f"  Row {idx} ({tracking}): matched invoice with {len(matched_invoice.items)} items")
            
            waybill_xml = generate_waybill_xml(row, shipment_info, idx)
            zf.writestr(f"waybills/{tracking}_waybill.xml", waybill_xml)
            
            declaration_xml = generate_declaration_xml(row, shipment_info, idx, invoice=matched_invoice)
            zf.writestr(f"declarations/{tracking}_declaration.xml", declaration_xml)
    
    zip_buffer.seek(0)
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=asycuda_xmls_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        }
    )


@app.post("/generate-waybills")
async def generate_waybills(
    xlsx_file: UploadFile = File(...),
    master_awb: str = Form(...),
    voyage_number: str = Form(""),
    date_of_departure: str = Form(""),
    carrier_name: str = Form(""),
):
    """Generate ONLY House Waybill XMLs from uploaded spreadsheet.
    No invoice PDFs needed. No manifest reference needed."""
    
    if not master_awb or master_awb.strip() == "":
        raise HTTPException(status_code=400, detail="Master AWB / BOL number is required and cannot be empty.")
    
    xlsx_bytes = await xlsx_file.read()
    rows = parse_xlsx(xlsx_bytes)
    
    if not rows:
        raise HTTPException(status_code=400, detail="No valid data rows found in the spreadsheet.")
    
    shipment_info = {
        "master_awb": master_awb.strip(),
        "voyage_number": voyage_number.strip(),
        "date_of_departure": date_of_departure.strip(),
        "carrier_name": carrier_name.strip(),
        "manifest_reference": "",
        "port_of_loading_code": "USMIA",
        "port_of_loading_name": "MIAMI, USA",
    }
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, row in enumerate(rows, start=1):
            tracking = safe_str(row.get("tracking_number", f"ROW_{idx}"))
            waybill_xml = generate_waybill_xml(row, shipment_info, idx)
            zf.writestr(f"{tracking}_waybill.xml", waybill_xml)
    
    zip_buffer.seek(0)
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=waybills_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        }
    )


@app.post("/generate-declarations")
async def generate_declarations(
    xlsx_file: UploadFile = File(...),
    master_awb: str = Form(...),
    manifest_reference: str = Form(...),
    voyage_number: str = Form(""),
    date_of_departure: str = Form(""),
    carrier_name: str = Form(""),
    pdf_files: List[UploadFile] = File(default=[]),
):
    """Generate ONLY Declaration XMLs from uploaded spreadsheet and optional invoice PDFs.
    Requires manifest_reference (obtained after waybill upload to ASYCUDA)."""
    
    if not master_awb or master_awb.strip() == "":
        raise HTTPException(status_code=400, detail="Master AWB / BOL number is required and cannot be empty.")
    
    if not manifest_reference or manifest_reference.strip() == "":
        raise HTTPException(status_code=400, detail="Manifest Reference Number is required for declarations. Upload waybills first to obtain this from ASYCUDA.")
    
    xlsx_bytes = await xlsx_file.read()
    rows = parse_xlsx(xlsx_bytes)
    
    if not rows:
        raise HTTPException(status_code=400, detail="No valid data rows found in the spreadsheet.")
    
    shipment_info = {
        "master_awb": master_awb.strip(),
        "voyage_number": voyage_number.strip(),
        "date_of_departure": date_of_departure.strip(),
        "carrier_name": carrier_name.strip(),
        "manifest_reference": manifest_reference.strip(),
        "port_of_loading_code": "USMIA",
        "port_of_loading_name": "MIAMI, USA",
    }
    
    invoices = {}
    if pdf_files:
        pdf_data = []
        for pdf_file in pdf_files:
            if pdf_file.filename and pdf_file.filename.endswith('.pdf'):
                pdf_bytes = await pdf_file.read()
                if pdf_bytes:
                    pdf_data.append((pdf_file.filename, pdf_bytes))
        if pdf_data:
            print(f"Parsing {len(pdf_data)} invoice PDF(s)...")
            invoices = parse_all_invoices(pdf_data)
            print(f"Successfully parsed {len(invoices)} invoice(s) with item data")
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, row in enumerate(rows, start=1):
            tracking = safe_str(row.get("tracking_number", f"ROW_{idx}"))
            matched_invoice = match_invoice_to_row(tracking, invoices)
            if matched_invoice:
                print(f"  Row {idx} ({tracking}): matched invoice with {len(matched_invoice.items)} items")
            
            declaration_xml = generate_declaration_xml(row, shipment_info, idx, invoice=matched_invoice)
            zf.writestr(f"{tracking}_declaration.xml", declaration_xml)
    
    zip_buffer.seek(0)
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=declarations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ASYCUDA XML Generator", "invoice_parsing": "enabled"}



@app.post("/debug-pdf")
async def debug_pdf(pdf_file: UploadFile = File(...)):
    """Debug endpoint: upload a single PDF and see what pdfplumber extracts + parser results."""
    import pdfplumber
    
    pdf_bytes = await pdf_file.read()
    filename = pdf_file.filename or "unknown.pdf"
    
    result = {
        "filename": filename,
        "file_size": len(pdf_bytes),
        "tracking_extracted": extract_tracking_from_filename(filename),
        "pages": [],
        "all_text": "",
        "tables_found": 0,
        "parse_result": None,
    }
    
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            all_text = ""
            for page_num, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                all_text += page_text + "\n"
                tables = page.extract_tables()
                
                page_info = {
                    "page_number": page_num + 1,
                    "text": page_text,
                    "text_lines": page_text.split("\n") if page_text else [],
                    "tables_count": len(tables),
                    "tables": tables,
                }
                result["pages"].append(page_info)
                result["tables_found"] += len(tables)
            
            result["all_text"] = all_text
        
        invoice = parse_invoice_pdf(pdf_bytes, filename)
        if invoice:
            result["parse_result"] = {
                "tracking": invoice.tracking_number,
                "buyer": invoice.buyer_name,
                "date": invoice.invoice_date,
                "grand_total": invoice.grand_total,
                "items_count": len(invoice.items),
                "items": [
                    {"description": item.description, "quantity": item.quantity, "total_value": item.total_value}
                    for item in invoice.items
                ]
            }
        else:
            result["parse_result"] = {"error": "No items could be extracted from this PDF"}
    
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)}"
    
    return result


@app.post("/preview")
async def preview_xlsx(
    xlsx_file: UploadFile = File(...),
    master_awb: str = Form(""),
    voyage_number: str = Form(""),
    date_of_departure: str = Form(""),
    carrier_name: str = Form(""),
    manifest_reference: str = Form(""),
    pdf_files: List[UploadFile] = File(default=[]),
):
    """Preview parsed spreadsheet data with invoice match info."""
    xlsx_bytes = await xlsx_file.read()
    rows = parse_xlsx(xlsx_bytes)
    
    if not rows:
        raise HTTPException(status_code=400, detail="No valid data rows found in the spreadsheet.")
    
    invoices = {}
    if pdf_files:
        pdf_data = []
        for pdf_file in pdf_files:
            if pdf_file.filename and pdf_file.filename.endswith('.pdf'):
                pdf_bytes = await pdf_file.read()
                if pdf_bytes:
                    pdf_data.append((pdf_file.filename, pdf_bytes))
        if pdf_data:
            invoices = parse_all_invoices(pdf_data)
    
    preview_rows = []
    for row in rows:
        tracking = safe_str(row.get("tracking_number", ""))
        items_desc = parse_bracketed_array(safe_str(row.get("items_description", "")))
        items_count = len(items_desc) if items_desc else (int(row.get("items", 0)) if row.get("items") else 0)
        
        matched_invoice = match_invoice_to_row(tracking, invoices)
        has_invoice = matched_invoice is not None
        invoice_items_count = len(matched_invoice.items) if matched_invoice else 0
        invoice_total = matched_invoice.grand_total if matched_invoice else 0
        
        preview_rows.append({
            "tracking_number": tracking,
            "buyer_name": safe_str(row.get("buyer_name", "")),
            "weight": safe_str(row.get("weight", "")),
            "cif_verified": safe_str(row.get("cif_verified", "")),
            "items_count": items_count,
            "has_invoice": has_invoice,
            "invoice_items_count": invoice_items_count,
            "invoice_total": f"{invoice_total:.2f}" if invoice_total else ""
        })
    
    return {
        "rows": preview_rows,
        "total": len(preview_rows),
        "invoices_matched": sum(1 for r in preview_rows if r["has_invoice"]),
        "invoices_parsed": len(invoices)
    }



# ─── Customer Invoice PDF Generation ────────────────────────────────────────────

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, black, white, grey
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


def generate_customer_invoice_pdf(
    tracking_number: str,
    customer_name: str,
    address: str,
    telephone: str,
    email: str,
    arrival_date: str,
    shipper_name: str,
    shipper_invoice_number: str,
    description: str,
    total_order_value: float,
    customs_duties: float,
) -> bytes:
    """Generate a professional customer invoice PDF and return as bytes."""

    # Calculations
    AASPA_FEE = 10.00
    EXCHANGE_RATE = 2.6882
    fee = customs_duties * 0.05
    total_ec = customs_duties + AASPA_FEE + fee
    total_usd = total_ec / EXCHANGE_RATE
    today = datetime.now().strftime("%d %B %Y")

    # Colors
    dark_red = HexColor("#8B0000")
    gold = HexColor("#F39C12")
    light_grey_bg = HexColor("#F8F8F8")
    border_grey = HexColor("#DDDDDD")

    buffer = io.BytesIO()
    # Tighter margins to fit both sections on one A4 page
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()

    # Compact styles — reduced font sizes and spacing throughout
    style_company = ParagraphStyle(
        'Company', parent=styles['Normal'],
        fontSize=16, fontName='Helvetica-Bold', textColor=dark_red,
        spaceAfter=1,
    )
    style_address = ParagraphStyle(
        'Address', parent=styles['Normal'],
        fontSize=8, textColor=grey, spaceAfter=0,
    )
    style_title = ParagraphStyle(
        'Title', parent=styles['Normal'],
        fontSize=11, fontName='Helvetica-Bold', textColor=dark_red,
        alignment=TA_CENTER, spaceAfter=4, spaceBefore=4,
    )
    style_normal = ParagraphStyle(
        'NormalCustom', parent=styles['Normal'],
        fontSize=9, leading=11,
    )
    style_small = ParagraphStyle(
        'SmallCustom', parent=styles['Normal'],
        fontSize=8, leading=10, textColor=grey,
    )
    style_bold = ParagraphStyle(
        'BoldCustom', parent=styles['Normal'],
        fontSize=9, fontName='Helvetica-Bold', leading=11,
    )
    style_date_paid_label = ParagraphStyle(
        'DatePaidLabel', parent=styles['Normal'],
        fontSize=9, fontName='Helvetica-Bold', textColor=dark_red,
        alignment=TA_LEFT,
    )
    style_cut = ParagraphStyle(
        'Cut', parent=styles['Normal'],
        fontSize=8, textColor=grey, alignment=TA_CENTER,
        spaceBefore=2, spaceAfter=2,
    )
    style_delivery_header = ParagraphStyle(
        'DeliveryHeader', parent=styles['Normal'],
        fontSize=11, fontName='Helvetica-Bold', textColor=dark_red,
        spaceAfter=4,
    )
    style_italic_small = ParagraphStyle(
        'ItalicSmall', parent=styles['Normal'],
        fontSize=8, fontName='Helvetica-Oblique', leading=10,
        spaceBefore=4, spaceAfter=4,
    )
    style_sig = ParagraphStyle(
        'Signature', parent=styles['Normal'],
        fontSize=9, spaceBefore=10,
    )

    elements = []

    # ─── SECTION 1: CUSTOMER INVOICE ────────────────────────────────────────

    # Header
    elements.append(Paragraph("SAFE CARGO SERVICES", style_company))
    elements.append(Paragraph("Sandy Ground, Anguilla  |  Tel: (264) 498-0194", style_address))
    elements.append(Spacer(1, 2 * mm))

    # Gold line separator
    elements.append(HRFlowable(
        width="100%", thickness=2, color=gold,
        spaceBefore=1, spaceAfter=4,
    ))

    # Title
    elements.append(Paragraph("CUSTOMS CLEARANCE INVOICE", style_title))
    elements.append(Spacer(1, 2 * mm))

    # Two-column info row — wider columns to match new 180mm usable width
    info_data = [
        [
            Paragraph(f"<b>Date:</b> {today}", style_normal),
            Paragraph(f"<b>Invoice #:</b> {shipper_invoice_number}", style_normal),
        ],
        [
            Paragraph(f"<b>Tracking #:</b> {tracking_number}", style_normal),
            Paragraph("", style_normal),
        ],
    ]
    info_table = Table(info_data, colWidths=[90 * mm, 90 * mm])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 2 * mm))

    # Customer details box
    cust_data = [
        [Paragraph(f"<b>Customer Name:</b> {customer_name}", style_normal)],
        [Paragraph(f"<b>Address:</b> {address}", style_normal)],
        [Paragraph(f"<b>Telephone:</b> {telephone}", style_normal)],
        [Paragraph(f"<b>Email:</b> {email}", style_normal)],
    ]
    cust_table = Table(cust_data, colWidths=[180 * mm])
    cust_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, border_grey),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    elements.append(cust_table)
    elements.append(Spacer(1, 2 * mm))

    # Shipment details table
    ship_data = [
        [Paragraph("<b>Field</b>", style_bold), Paragraph("<b>Value</b>", style_bold)],
        [Paragraph("Shipper", style_normal), Paragraph(shipper_name, style_normal)],
        [Paragraph("Shipper Invoice #", style_normal), Paragraph(shipper_invoice_number, style_normal)],
        [Paragraph("Description of Goods", style_normal), Paragraph(description, style_normal)],
        [Paragraph("Total Order Value", style_normal), Paragraph(f"US$ {total_order_value:.2f}", style_normal)],
    ]
    ship_table = Table(ship_data, colWidths=[62 * mm, 118 * mm])
    ship_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), light_grey_bg),
        ('GRID', (0, 0), (-1, -1), 0.5, border_grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(ship_table)
    elements.append(Spacer(1, 2 * mm))

    # Charges table
    charges_data = [
        [Paragraph("<b>Description</b>", style_bold), Paragraph("<b>Amount</b>", style_bold)],
        [Paragraph("Customs Duties", style_normal), Paragraph(f"EC$ {customs_duties:.2f}", style_normal)],
        [Paragraph("AASPA Fee", style_normal), Paragraph(f"EC$ {AASPA_FEE:.2f}", style_normal)],
        [Paragraph("Import Clearance Service Fee (5%)", style_normal), Paragraph(f"EC$ {fee:.2f}", style_normal)],
        [Paragraph("<b>TOTAL DUE</b>", style_bold), Paragraph(f"<b>EC$ {total_ec:.2f}</b>", style_bold)],
        [Paragraph("<b>TOTAL DUE (USD)</b>", style_bold), Paragraph(f"<b>US$ {total_usd:.2f}</b>", style_bold)],
    ]
    charges_table = Table(charges_data, colWidths=[118 * mm, 62 * mm])
    charges_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), dark_red),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('GRID', (0, 0), (-1, -1), 0.5, border_grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('BACKGROUND', (0, 4), (-1, 5), HexColor("#FDF2E9")),
        ('LINEABOVE', (0, 4), (-1, 4), 1.5, dark_red),
    ]))
    elements.append(charges_table)
    elements.append(Spacer(1, 1 * mm))

    # Exchange rate note
    elements.append(Paragraph("Exchange Rate: EC$2.6882 = US$1.00", style_small))
    elements.append(Spacer(1, 2 * mm))

    # DATE PAID box — replaces the old PAID ✓ stamp
    date_paid_inner = [[
        Paragraph(
            '<b><font color="#8B0000">DATE PAID:</font></b>'
            '&nbsp;&nbsp;_______________________',
            style_date_paid_label,
        )
    ]]
    date_paid_table = Table(date_paid_inner, colWidths=[80 * mm])
    date_paid_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1.5, dark_red),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    # Right-align the DATE PAID box using a wrapper table
    date_paid_wrapper = Table([[None, date_paid_table]], colWidths=[100 * mm, 80 * mm])
    date_paid_wrapper.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(date_paid_wrapper)
    elements.append(Spacer(1, 3 * mm))

    # ─── DASHED SEPARATOR ───────────────────────────────────────────────────
    elements.append(HRFlowable(
        width="100%", thickness=0.5, color=grey,
        spaceBefore=2, spaceAfter=1, dash=[4, 4],
    ))
    elements.append(Paragraph("✂  CUT HERE", style_cut))
    elements.append(Spacer(1, 3 * mm))

    # ─── SECTION 2: DELIVERY TICKET ────────────────────────────────────────

    elements.append(Paragraph("SAFE CARGO SERVICES — DELIVERY TICKET", style_delivery_header))

    # Gold underline
    elements.append(HRFlowable(
        width="100%", thickness=1.5, color=gold,
        spaceBefore=0, spaceAfter=4,
    ))

    # Info fields
    delivery_data = [
        [Paragraph("<b>Date of Arrival:</b>", style_normal), Paragraph(arrival_date if arrival_date else "_______________", style_normal)],
        [Paragraph("<b>Tracking #:</b>", style_normal), Paragraph(tracking_number, style_normal)],
        [Paragraph("<b>Customer:</b>", style_normal), Paragraph(customer_name, style_normal)],
        [Paragraph("<b>Telephone:</b>", style_normal), Paragraph(telephone, style_normal)],
    ]
    delivery_table = Table(delivery_data, colWidths=[40 * mm, 140 * mm])
    delivery_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(delivery_table)
    elements.append(Spacer(1, 3 * mm))

    # Declaration text
    elements.append(Paragraph(
        "“I hereby confirm receipt of the above-mentioned goods and acknowledge "
        "payment of all applicable customs duties and fees.”",
        style_italic_small
    ))

    # Signature line — with double spacing above
    elements.append(Spacer(1, 6 * mm))
    elements.append(Paragraph(
        "Customer Signature: _______________________________&nbsp;&nbsp;&nbsp;&nbsp;"
        "Date: _______________",
        style_sig
    ))
    elements.append(Spacer(1, 6 * mm))

    # Comments section
    style_comments = ParagraphStyle(
        'Comments', parent=styles['Normal'],
        fontSize=9, leading=14,
    )
    elements.append(Paragraph("Comments:", style_comments))
    elements.append(Paragraph("_________________________________________________________________", style_comments))
    elements.append(Paragraph("_________________________________________________________________", style_comments))
    elements.append(Paragraph("_________________________________________________________________", style_comments))

    # Build PDF
    doc.build(elements)
    buffer.seek(0)
    return buffer.read()


@app.post("/generate-customer-invoice")
async def generate_customer_invoice(
    tracking_number: str = Form(...),
    customer_name: str = Form(...),
    address: str = Form(""),
    telephone: str = Form(""),
    email: str = Form(""),
    arrival_date: str = Form(""),
    shipper_name: str = Form(""),
    shipper_invoice_number: str = Form(""),
    description: str = Form(""),
    total_order_value: float = Form(0.0),
    customs_duties: float = Form(0.0),
):
    """Generate a customer invoice PDF and return it as a downloadable file."""
    pdf_bytes = generate_customer_invoice_pdf(
        tracking_number=tracking_number,
        customer_name=customer_name,
        address=address,
        telephone=telephone,
        email=email,
        arrival_date=arrival_date,
        shipper_name=shipper_name,
        shipper_invoice_number=shipper_invoice_number,
        description=description,
        total_order_value=total_order_value,
        customs_duties=customs_duties,
    )

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="SCS_Invoice_{tracking_number}.pdf"'
        }
    )


# Mount static files LAST so API routes take priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")
