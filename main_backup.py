import io
import re
import zipfile
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, field

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import StreamingResponse
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
    """Extract tracking number from an invoice PDF filename.
    
    Handles patterns like:
    - 20260821_custom_invoice_proform_MLBS000000218XX.pdf
    - invoice_MLBS000001037XX.pdf
    - MLBS000002602XX.pdf
    """
    # Remove extension
    name = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
    
    # Pattern: MLBS followed by digits and optional suffix (most specific for MLBS)
    mlbs_match = re.search(r'(MLBS\d+[A-Za-z]*)', name, re.IGNORECASE)
    if mlbs_match:
        return mlbs_match.group(1).upper()
    
    # General tracking patterns
    tracking_patterns = [
        r'([A-Z]{2,4}\d{6,}[A-Za-z0-9]*)',  # 2-4 caps + 6+ digits + optional suffix
    ]
    for pattern in tracking_patterns:
        match = re.search(pattern, name)
        if match:
            return match.group(1)
    
    # Fallback: last segment after underscore (before .pdf was already stripped)
    parts = name.split('_')
    if parts:
        return parts[-1].strip()
    
    return name


def parse_invoice_pdf(file_bytes: bytes, filename: str) -> Optional[InvoiceData]:
    """Parse an invoice PDF and extract item data.
    
    Attempts multiple parsing strategies (in order):
    1. Table-based extraction (structured tables with borders)
    2. Text-based regex extraction (various line formats)
    3. Dollar-amount line scanning (very permissive fallback)
    """
    tracking = extract_tracking_from_filename(filename)
    invoice = InvoiceData(tracking_number=tracking)
    
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            all_text = ""
            all_tables = []
            
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                all_text += page_text + "\n"
                
                # Try table extraction
                tables = page.extract_tables()
                if tables:
                    all_tables.extend(tables)
            
            print(f"  [PDF DEBUG] {filename}: {len(all_tables)} tables, {len(all_text)} chars text")
            
            # Strategy 1: Parse from tables (if pdfplumber detected table structures)
            if all_tables:
                invoice = _parse_from_tables(all_tables, all_text, tracking)
                if invoice.items:
                    print(f"  [PDF DEBUG] Strategy 1 (tables) found {len(invoice.items)} items")
            
            # Strategy 2: Parse from text using regex patterns
            if not invoice.items:
                invoice = _parse_from_text(all_text, tracking)
                if invoice.items:
                    print(f"  [PDF DEBUG] Strategy 2 (text regex) found {len(invoice.items)} items")
            
            # Strategy 3: Very permissive - scan for lines with dollar amounts
            if not invoice.items:
                invoice = _parse_from_dollar_lines(all_text, tracking)
                if invoice.items:
                    print(f"  [PDF DEBUG] Strategy 3 (dollar scan) found {len(invoice.items)} items")
            
            if not invoice.items:
                print(f"  [PDF DEBUG] ALL STRATEGIES FAILED for {filename}")
                print(f"  [PDF DEBUG] First 500 chars of text: {repr(all_text[:500])}")
            
            # Extract invoice date
            date_match = re.search(
                r'(?:Invoice\s*Date|Date)[:\s]*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+\w+\s+\d{4})',
                all_text, re.IGNORECASE
            )
            if date_match:
                invoice.invoice_date = date_match.group(1).strip()
            
            # Extract grand total
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
            
            # Extract buyer name
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
        
        # Find header row and identify columns
        header_row = None
        desc_col = qty_col = price_col = total_col = None
        
        for row_idx, row in enumerate(table):
            if not row:
                continue
            row_lower = [str(cell).lower().strip() if cell else "" for cell in row]
            
            # Look for header indicators
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
                        if desc_col is None:  # Take first match
                            desc_col = ci
                    elif any(h in cell for h in ['qty', 'quantity', 'pcs', 'units', 'pieces']):
                        qty_col = ci
                    elif any(h in cell for h in ['total', 'amount', 'subtotal', 'ext']) and 'unit' not in cell and 'grand' not in cell:
                        total_col = ci
                    elif any(h in cell for h in ['price', 'unit', 'rate', 'cost', 'each']):
                        price_col = ci
                break
        
        if header_row is None or desc_col is None:
            # Try a heuristic: assume first row is header, second column is description
            if len(table) >= 3 and len(table[0]) >= 3:
                header_row = 0
                # Guess columns by position (typical: No | Desc | Qty | Price | Total)
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
        
        # Parse data rows after header
        for row in table[header_row + 1:]:
            if not row or len(row) <= desc_col:
                continue
            
            desc = str(row[desc_col]).strip() if row[desc_col] else ""
            if not desc or desc.lower() in ('', 'total', 'grand total', 'subtotal', 'none', 'null'):
                continue
            # Skip if description is purely numeric (likely a total row)
            if re.match(r'^[\d.,\s$]+$', desc):
                continue
            
            # Parse quantity
            qty = 1
            if qty_col is not None and qty_col < len(row) and row[qty_col]:
                try:
                    qty_str = re.sub(r'[^\d.]', '', str(row[qty_col]))
                    qty = int(float(qty_str)) if qty_str else 1
                except (ValueError, TypeError):
                    qty = 1
            
            # Parse total value (prefer total column over price * qty)
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
    """Parse invoice items from raw text using regex patterns.
    
    Handles both multi-space and single-space separated fields as produced
    by pdfplumber text extraction.
    """
    invoice = InvoiceData(tracking_number=tracking)
    
    # Patterns ordered from most specific to most permissive.
    patterns = [
        # 4-column with line number: "1 description qty unit_price total"
        (4, r'^\s*\d+[\s.)]+(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$'),
        # 4-column: "description qty unit_price total" (single or multi-space)
        (4, r'^(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$'),
        # 3-column with line number: "1 description qty price"
        (3, r'^\s*\d+[\s.)]+(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s*$'),
        # 3-column: "description qty price" (no separate total)
        (3, r'^(.+?)\s+(\d+)\s+\$?([\d,]+\.\d{2})\s*$'),
        # 2-column with line number: "1 description price" (qty=1 assumed)
        (2, r'^\s*\d+[\s.)]+(.+?)\s+\$?([\d,]+\.\d{2})\s*$'),
        # Parenthetical: "description (qty, $price)"
        (3, r"([A-Za-z][A-Za-z\s']+?)\s*\((\d+)\s*,\s*\$?([\d,]+\.\d{2})\)"),
    ]
    
    # Keywords that indicate a non-item line (headers, footers, totals)
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
        
        # Skip obvious header/footer/meta lines
        if any(skip in line_lower for skip in skip_keywords):
            # But don't skip if the line also has a price pattern (item might contain a keyword)
            if not re.search(r'\$?\d+\.\d{2}', line):
                continue
            # If it starts with a keyword (not an item line), skip
            if any(line_lower.startswith(skip) for skip in skip_keywords):
                continue
        
        # Skip lines that are ONLY a total
        if re.match(r'^\s*(grand\s*)?total[:\s]*\$?[\d,.]+\s*$', line, re.IGNORECASE):
            continue
        # Skip lines that are just a number or price
        if re.match(r'^\s*\$?[\d,. ]+$', line):
            continue
        
        for ncols, pattern in patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                groups = match.groups()
                desc = groups[0].strip()
                
                # Validate description: must start with a letter and be at least 2 chars
                if not desc or len(desc) < 2 or not re.match(r'[A-Za-z]', desc):
                    continue
                
                # Skip if description looks like a header keyword
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
                break  # Move to next line after first pattern match
    
    return invoice


def _parse_from_dollar_lines(text: str, tracking: str) -> InvoiceData:
    """Fallback: scan for lines containing text + dollar amounts.
    
    Very permissive — looks for any line with alphabetic text followed by a dollar amount.
    Used when tables and structured text patterns both fail.
    """
    invoice = InvoiceData(tracking_number=tracking)
    
    # Skip lines that are clearly not items
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
        
        # Skip non-item lines
        if any(line_lower.startswith(s) for s in skip_starts):
            continue
        if 'total' in line_lower and not re.search(r'[a-zA-Z]{3,}.*\d', line_lower.replace('total', '')):
            continue
        
        # Look for pattern: text followed by dollar amount(s)
        # Match: "some description ... $XX.XX" or "some description ... XX.XX"
        match = re.match(
            r'^\s*\d*[\s.)]*([A-Za-z][A-Za-z\s\'\-&,./]+?)\s+(?:\d+\s+)?(?:\$?[\d,]+\.\d{2}\s+)*\$?([\d,]+\.\d{2})\s*$',
            line
        )
        if match:
            desc = match.group(1).strip()
            last_amount = float(match.group(2).replace(',', ''))
            
            # Validate
            if len(desc) >= 2 and last_amount > 0 and last_amount < 10000:
                found_items.append((desc, 1, last_amount))
    
    # Only use this strategy if we found a reasonable number of items (2+)
    if len(found_items) >= 2:
        for desc, qty, total in found_items:
            invoice.items.append(InvoiceItem(
                description=desc,
                quantity=qty,
                total_value=round(total, 2)
            ))
    
    return invoice


def parse_all_invoices(pdf_files_data: List[tuple]) -> dict:
    """Parse all uploaded PDFs and return a dict keyed by tracking number.
    
    Args:
        pdf_files_data: List of (filename, file_bytes) tuples
    
    Returns:
        Dict mapping tracking_number -> InvoiceData
    """
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
    """Match an invoice to a spreadsheet row by tracking number.
    
    Supports exact match, case-insensitive, partial/substring, and digit-based matching.
    """
    if not invoices:
        return None
    
    row_tracking_clean = safe_str(row_tracking).strip().upper()
    
    if not row_tracking_clean:
        return None
    
    # Exact match (case-insensitive)
    for inv_tracking, invoice in invoices.items():
        if inv_tracking.upper() == row_tracking_clean:
            print(f"    [MATCH] Exact match: {row_tracking_clean} == {inv_tracking}")
            return invoice
    
    # Partial match: check if row tracking is contained in invoice tracking or vice versa
    for inv_tracking, invoice in invoices.items():
        inv_upper = inv_tracking.upper()
        if row_tracking_clean in inv_upper or inv_upper in row_tracking_clean:
            print(f"    [MATCH] Partial match: {row_tracking_clean} <-> {inv_tracking}")
            return invoice
    
    # Digit-based match: compare just the numeric parts
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
    """Parse bracketed arrays like [item1][item2][item3] into a list."""
    if not value or str(value).strip() == "":
        return []
    value = str(value).strip()
    # Try bracketed format first
    matches = re.findall(r'\[([^\]]*)\]', value)
    if matches:
        return [m.strip() for m in matches]
    # Fall back to comma-separated
    return [v.strip() for v in value.split(',') if v.strip()]


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
    # Fix XML declaration: single quotes -> double quotes
    text = text.replace("<?xml version='1.0' encoding='UTF-8' standalone='no'?>",
                        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
    # Fix indentation: lxml uses 2 spaces, ASYCUDA expects 4 spaces
    lines = text.split('\n')
    fixed_lines = []
    for line in lines:
        # Count leading spaces
        stripped = line.lstrip(' ')
        spaces = len(line) - len(stripped)
        # Double the indentation (2 -> 4 spaces per level)
        fixed_lines.append(' ' * (spaces * 2) + stripped)
    return '\n'.join(fixed_lines).encode('utf-8')

def generate_waybill_xml(row_data: dict, shipment_info: dict, row_index: int) -> bytes:
    """Generate House Waybill XML matching the exact Anguilla ASYCUDA World template."""
    
    voyage = shipment_info.get("voyage_number", "")
    master_awb = shipment_info["master_awb"]
    
    # Date in M/D/YY format as required by ASYCUDA
    raw_date = shipment_info.get("date_of_departure", "")
    try:
        from datetime import datetime
        dt = datetime.strptime(raw_date, "%Y-%m-%d")
        formatted_date = f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
    except Exception:
        formatted_date = raw_date
    
    # Previous_document_reference = voyage_number-line_number
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
    
    # Build XML as a string to exactly match template structure (no indentation, double quotes)
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
    # Uppercase all text content between XML tags
    import re as _re
    def _upper_text(m):
        tag_open, text, tag_close = m.group(1), m.group(2), m.group(3)
        return tag_open + text.upper() + tag_close
    xml_str = _re.sub(r'(>)([^<>]+)(<)', _upper_text, xml_str)
    return xml_str.encode('utf-8')



def generate_declaration_xml(row_data: dict, shipment_info: dict, row_index: int,
                             invoice: Optional[InvoiceData] = None) -> bytes:
    """Generate Customs Declaration XML (ASYCUDA SAD) for a single row.
    
    If an invoice is provided, use per-item values from the invoice instead of
    evenly distributing FOB. Invoice item descriptions are preferred over
    spreadsheet bracketed arrays, but HS codes always come from spreadsheet.
    """
    root = etree.Element("ASYCUDA")
    
    # Export_release
    export_rel = etree.SubElement(root, "Export_release")
    set_element_text(export_rel, "Date_of_exit")
    set_element_text(export_rel, "Time_of_exit")
    set_element_text(export_rel, "Actual_office_of_exit_code", use_null=True)
    set_element_text(export_rel, "Actual_office_of_exit_name", use_null=True)
    set_element_text(export_rel, "Exit_reference", use_null=True)
    set_element_text(export_rel, "Comments", use_null=True)
    
    # Assessment_notice
    assess = etree.SubElement(root, "Assessment_notice")
    set_element_text(assess, "Registration_year", use_null=True)
    set_element_text(assess, "Assessment_year", use_null=True)
    set_element_text(assess, "Total_item_taxes")
    set_element_text(assess, "Statement_number", use_null=True)
    set_element_text(assess, "Statement_date")
    set_element_text(assess, "Statement_serial", use_null=True)
    set_element_text(assess, "Items_taxes")
    set_element_text(assess, "Global_taxes")
    
    # Determine items source: invoice (preferred) or spreadsheet
    items_hs = parse_bracketed_array(safe_str(row_data.get("items_hs_codes", "")))
    
    if invoice and invoice.items:
        # Use invoice data for items
        item_count = len(invoice.items)
        use_invoice = True
    else:
        # Fallback to spreadsheet data
        items_desc = parse_bracketed_array(safe_str(row_data.get("items_description", "")))
        items_qty = parse_bracketed_array(safe_str(row_data.get("items_quantities", "")))
        item_count = max(len(items_desc), 1)
        use_invoice = False
    
    # Property
    prop = etree.SubElement(root, "Property")
    set_element_text(prop, "Sad_flow", "I")
    forms = etree.SubElement(prop, "Forms")
    set_element_text(forms, "Number_of_the_form", "1")
    set_element_text(forms, "Total_number_of_forms", "1")
    nbers = etree.SubElement(prop, "Nbers")
    set_element_text(nbers, "Number_of_loading_lists", "0")
    set_element_text(nbers, "Total_number_of_items", str(item_count))
    set_element_text(nbers, "Total_number_of_packages", str(item_count))
    set_element_text(prop, "Place_of_declaration", use_null=True)
    set_element_text(prop, "Date_of_declaration")
    set_element_text(prop, "Selected_page", "1")
    
    # Identification
    ident = etree.SubElement(root, "Identification")
    office = etree.SubElement(ident, "Office_segment")
    set_element_text(office, "Customs_clearance_office_code", "00RB")
    set_element_text(office, "Customs_Clearance_office_name", "ROAD BAY")
    
    type_seg = etree.SubElement(ident, "Type")
    set_element_text(type_seg, "Type_of_declaration", "IM")
    set_element_text(type_seg, "Declaration_gen_procedure_code", "4")
    set_element_text(type_seg, "Type_of_transit_document", use_null=True)
    
    set_element_text(ident, "Manifest_reference_number", shipment_info.get("manifest_reference", ""))
    
    reg = etree.SubElement(ident, "Registration")
    set_element_text(reg, "Serial_number", use_null=True)
    set_element_text(reg, "Number")
    set_element_text(reg, "Date")
    
    assessment = etree.SubElement(ident, "Assessment")
    set_element_text(assessment, "Serial_number", use_null=True)
    set_element_text(assessment, "Number")
    set_element_text(assessment, "Date")
    
    receipt = etree.SubElement(ident, "receipt")
    set_element_text(receipt, "Serial_number", use_null=True)
    set_element_text(receipt, "Number")
    set_element_text(receipt, "Date")
    
    # Traders
    traders = etree.SubElement(root, "Traders")
    
    exporter = etree.SubElement(traders, "Exporter")
    set_element_text(exporter, "Exporter_code")
    exporter_name = ", ".join(filter(None, [
        safe_str(row_data.get("shipper", "")),
        safe_str(row_data.get("shipper_address1", "")),
        safe_str(row_data.get("shipper_city", "")),
        safe_str(row_data.get("shipper_country", ""))
    ]))
    set_element_text(exporter, "Exporter_name", exporter_name)
    
    consignee = etree.SubElement(traders, "Consignee")
    set_element_text(consignee, "Consignee_code", "999")
    consignee_name = f"{safe_str(row_data.get('buyer_name', ''))}\n{safe_str(row_data.get('buyer_address1', ''))}\n{safe_str(row_data.get('buyer_city', ''))}, {safe_str(row_data.get('buyer_state', ''))}"
    set_element_text(consignee, "Consignee_name", consignee_name)
    
    financial = etree.SubElement(traders, "Financial")
    set_element_text(financial, "Financial_code", use_null=True)
    set_element_text(financial, "Financial_name", "EVETTE HARRIGAN\nSOUTH HILL")
    
    # Declarant
    declarant = etree.SubElement(root, "Declarant")
    set_element_text(declarant, "Declarant_code", "DC2114")
    set_element_text(declarant, "Declarant_name", "Safe Cargo Services\nSandy Ground\nAnguilla")
    set_element_text(declarant, "Declarant_representative", "Evette Harrigan")
    
    ref = etree.SubElement(declarant, "Reference")
    # Use invoice date year if available, otherwise current year
    ref_year = str(datetime.now().year)
    if invoice and invoice.invoice_date:
        try:
            # Try to extract year from invoice date
            date_year_match = re.search(r'(\d{4})', invoice.invoice_date)
            if date_year_match:
                ref_year = date_year_match.group(1)
        except Exception:
            pass
    set_element_text(ref, "Year", ref_year)
    tracking = safe_str(row_data.get("tracking_number", ""))
    set_element_text(ref, "Number", f"EVETTE-{tracking}")
    
    # General_information
    gen_info = etree.SubElement(root, "General_information")
    country = etree.SubElement(gen_info, "Country")
    set_element_text(country, "Country_first_destination", "US")
    set_element_text(country, "Trading_country", "US")
    
    export_c = etree.SubElement(country, "Export")
    shipper_country = safe_str(row_data.get("shipper_country", "US"))
    country_code_map = {
        "US": "US", "USA": "US", "UNITED STATES": "US",
        "CN": "CN", "CHINA": "CN",
        "UK": "GB", "UNITED KINGDOM": "GB", "GB": "GB",
        "CA": "CA", "CANADA": "CA",
    }
    shipper_country_code = country_code_map.get(shipper_country.upper(), shipper_country[:2].upper() if shipper_country else "US")
    set_element_text(export_c, "Export_country_code", shipper_country_code)
    set_element_text(export_c, "Export_country_name", shipper_country)
    set_element_text(export_c, "Export_country_region")
    
    dest = etree.SubElement(country, "Destination")
    set_element_text(dest, "Destination_country_code", "AI")
    set_element_text(dest, "Destination_country_name", "Anguilla")
    set_element_text(dest, "Destination_country_region")
    
    set_element_text(country, "Country_of_origin_name", shipper_country)
    
    cif_val = safe_str(row_data.get("cif_verified", "0"))
    set_element_text(gen_info, "Value_details", cif_val)
    set_element_text(gen_info, "CAP")
    set_element_text(gen_info, "Additional_information", use_null=True)
    set_element_text(gen_info, "Comments_free_text")
    
    # Transport
    transport = etree.SubElement(root, "Transport")
    means = etree.SubElement(transport, "Means_of_transport")
    
    carrier_name = shipment_info.get("carrier_name", "")
    
    dep_arr = etree.SubElement(means, "Departure_arrival_information")
    set_element_text(dep_arr, "Identity", carrier_name)
    set_element_text(dep_arr, "Nationality", "AI")
    
    border_info = etree.SubElement(means, "Border_information")
    set_element_text(border_info, "Identity", carrier_name)
    set_element_text(border_info, "Nationality", "AI")
    set_element_text(border_info, "Mode", "4")
    
    set_element_text(means, "Inland_mode_of_transport", use_null=True)
    
    set_element_text(transport, "Container_flag", "false")
    
    delivery = etree.SubElement(transport, "Delivery_terms")
    ddp_ddu = safe_str(row_data.get("ddp_ddu", "DDP"))
    set_element_text(delivery, "Code", ddp_ddu)
    set_element_text(delivery, "Place", "ANGUILLA")
    set_element_text(delivery, "Situation")
    
    border_office = etree.SubElement(transport, "Border_office")
    set_element_text(border_office, "Code", "00RB")
    set_element_text(border_office, "Name", "ROAD BAY")
    
    place_loading = etree.SubElement(transport, "Place_of_loading")
    set_element_text(place_loading, "Code", "AIAXA")
    set_element_text(place_loading, "Name", "ANGUILLA")
    set_element_text(place_loading, "Country", use_null=True)
    
    set_element_text(transport, "Location_of_goods", "RBW2")
    
    # ─── Items section ──────────────────────────────────────────────────────────
    weight = float(safe_str(row_data.get("weight", "0")) or "0")
    fob = float(safe_str(row_data.get("fob_verified", "0")) or "0")
    
    weight_per_item = weight / item_count if item_count > 0 else 0
    
    if use_invoice:
        # Use invoice per-item values
        for i, inv_item in enumerate(invoice.items):
            item_elem = etree.SubElement(root, "Item")
            set_element_text(item_elem, "Item_number", str(i + 1))
            
            # Use invoice description (preferred over spreadsheet)
            desc = inv_item.description
            set_element_text(item_elem, "Goods_description", desc)
            
            procedure = etree.SubElement(item_elem, "Procedure")
            set_element_text(procedure, "Requested_procedure", "40")
            set_element_text(procedure, "Previous_procedure", "00")
            set_element_text(procedure, "National_procedure", use_null=True)
            
            commodity = etree.SubElement(item_elem, "Commodity")
            # HS codes always from spreadsheet
            hs_code = items_hs[i] if i < len(items_hs) else ""
            set_element_text(commodity, "HS_code", hs_code)
            set_element_text(commodity, "Description", desc)
            
            qty1 = etree.SubElement(item_elem, "Quantity_1")
            set_element_text(qty1, "Net_mass", f"{weight_per_item:.2f}")
            
            packages = etree.SubElement(item_elem, "Packages")
            set_element_text(packages, "Kind_of_packages_code", "PK")
            set_element_text(packages, "Number_of_packages", str(inv_item.quantity))
            set_element_text(packages, "Shipping_marks", tracking)
            
            # Valuation_item — use invoice per-item total_value
            val_item = etree.SubElement(item_elem, "Valuation_item")
            
            item_value = inv_item.total_value
            
            item_price = etree.SubElement(val_item, "item_price")
            set_element_text(item_price, "Amount_national_currency", f"{item_value:.2f}")
            set_element_text(item_price, "Amount_foreign_currency", f"{item_value:.2f}")
            set_element_text(item_price, "Currency_code", "USD")
            set_element_text(item_price, "Currency_name", "No foreign currency")
            set_element_text(item_price, "Currency_rate", "2.6882")
            
            item_freight = etree.SubElement(val_item, "item_freight")
            set_element_text(item_freight, "Amount_national_currency", "0.0")
            set_element_text(item_freight, "Amount_foreign_currency", "0.0")
            set_element_text(item_freight, "Currency_code", "USD")
            set_element_text(item_freight, "Currency_name", "No foreign currency")
            set_element_text(item_freight, "Currency_rate", "2.6882")
            
            item_ins = etree.SubElement(val_item, "item_insurance")
            set_element_text(item_ins, "Amount_national_currency", "0.0")
            set_element_text(item_ins, "Amount_foreign_currency", "0.0")
            set_element_text(item_ins, "Currency_code", "USD")
            set_element_text(item_ins, "Currency_name", "No foreign currency")
            set_element_text(item_ins, "Currency_rate", "2.6882")
            
            item_other = etree.SubElement(val_item, "item_other_cost")
            set_element_text(item_other, "Amount_national_currency", "0.0")
            set_element_text(item_other, "Amount_foreign_currency", "0.0")
            set_element_text(item_other, "Currency_code", "USD")
            set_element_text(item_other, "Currency_name", "No foreign currency")
            set_element_text(item_other, "Currency_rate", "2.6882")
            
            item_ded = etree.SubElement(val_item, "item_deduction")
            set_element_text(item_ded, "Amount_national_currency", "0.0")
            set_element_text(item_ded, "Amount_foreign_currency", "0.0")
            set_element_text(item_ded, "Currency_code", use_null=True)
            set_element_text(item_ded, "Currency_name", "No foreign currency")
            set_element_text(item_ded, "Currency_rate", "0.0")
    else:
        # Fallback: evenly distribute FOB across items from spreadsheet
        fob_per_item = fob / item_count if item_count > 0 else 0
        
        for i in range(item_count):
            item_elem = etree.SubElement(root, "Item")
            set_element_text(item_elem, "Item_number", str(i + 1))
            
            desc = items_desc[i] if i < len(items_desc) else ""
            set_element_text(item_elem, "Goods_description", desc)
            
            procedure = etree.SubElement(item_elem, "Procedure")
            set_element_text(procedure, "Requested_procedure", "40")
            set_element_text(procedure, "Previous_procedure", "00")
            set_element_text(procedure, "National_procedure", use_null=True)
            
            commodity = etree.SubElement(item_elem, "Commodity")
            hs_code = items_hs[i] if i < len(items_hs) else ""
            set_element_text(commodity, "HS_code", hs_code)
            set_element_text(commodity, "Description", desc)
            
            qty1 = etree.SubElement(item_elem, "Quantity_1")
            set_element_text(qty1, "Net_mass", f"{weight_per_item:.2f}")
            
            packages = etree.SubElement(item_elem, "Packages")
            set_element_text(packages, "Kind_of_packages_code", "PK")
            item_qty = items_qty[i] if i < len(items_qty) else "1"
            set_element_text(packages, "Number_of_packages", str(item_qty))
            set_element_text(packages, "Shipping_marks", tracking)
            
            val_item = etree.SubElement(item_elem, "Valuation_item")
            
            item_price = etree.SubElement(val_item, "item_price")
            set_element_text(item_price, "Amount_national_currency", f"{fob_per_item:.2f}")
            set_element_text(item_price, "Amount_foreign_currency", f"{fob_per_item:.2f}")
            set_element_text(item_price, "Currency_code", "USD")
            set_element_text(item_price, "Currency_name", "No foreign currency")
            set_element_text(item_price, "Currency_rate", "2.6882")
            
            item_freight = etree.SubElement(val_item, "item_freight")
            set_element_text(item_freight, "Amount_national_currency", "0.0")
            set_element_text(item_freight, "Amount_foreign_currency", "0.0")
            set_element_text(item_freight, "Currency_code", "USD")
            set_element_text(item_freight, "Currency_name", "No foreign currency")
            set_element_text(item_freight, "Currency_rate", "2.6882")
            
            item_ins = etree.SubElement(val_item, "item_insurance")
            set_element_text(item_ins, "Amount_national_currency", "0.0")
            set_element_text(item_ins, "Amount_foreign_currency", "0.0")
            set_element_text(item_ins, "Currency_code", "USD")
            set_element_text(item_ins, "Currency_name", "No foreign currency")
            set_element_text(item_ins, "Currency_rate", "2.6882")
            
            item_other = etree.SubElement(val_item, "item_other_cost")
            set_element_text(item_other, "Amount_national_currency", "0.0")
            set_element_text(item_other, "Amount_foreign_currency", "0.0")
            set_element_text(item_other, "Currency_code", "USD")
            set_element_text(item_other, "Currency_name", "No foreign currency")
            set_element_text(item_other, "Currency_rate", "2.6882")
            
            item_ded = etree.SubElement(val_item, "item_deduction")
            set_element_text(item_ded, "Amount_national_currency", "0.0")
            set_element_text(item_ded, "Amount_foreign_currency", "0.0")
            set_element_text(item_ded, "Currency_code", use_null=True)
            set_element_text(item_ded, "Currency_name", "No foreign currency")
            set_element_text(item_ded, "Currency_rate", "0.0")
    
    # Vehicle_List
    set_element_text(root, "Vehicle_List")
    
    tree = etree.ElementTree(root)
    output = io.BytesIO()
    tree.write(output, xml_declaration=True, encoding="UTF-8", pretty_print=True, standalone=False)
    return fix_xml_output(output.getvalue())


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
    """Generate ASYCUDA XML files from uploaded spreadsheet and optional invoice PDFs."""
    
    if not master_awb or master_awb.strip() == "":
        raise HTTPException(status_code=400, detail="Master AWB / BOL number is required and cannot be empty.")
    
    # Read xlsx
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
    
    # Parse invoice PDFs if provided
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
    
    # Generate ZIP
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, row in enumerate(rows, start=1):
            tracking = safe_str(row.get("tracking_number", f"ROW_{idx}"))
            
            # Try to match an invoice to this row
            matched_invoice = match_invoice_to_row(tracking, invoices)
            if matched_invoice:
                print(f"  Row {idx} ({tracking}): matched invoice with {len(matched_invoice.items)} items")
            
            # Generate waybill XML (unchanged - doesn't use invoice pricing)
            waybill_xml = generate_waybill_xml(row, shipment_info, idx)
            zf.writestr(f"waybills/{tracking}_waybill.xml", waybill_xml)
            
            # Generate declaration XML (uses invoice if matched)
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
        
        # Try parsing
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
    
    # Parse PDFs for preview matching info
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
        
        # Check if invoice matched
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


# Mount static files LAST so API routes take priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")
