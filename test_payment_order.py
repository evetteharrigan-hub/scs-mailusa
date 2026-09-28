"""Payment order parser and PDF upload regression tests."""

import pymupdf
from fastapi.testclient import TestClient

from main import app, parse_payment_order_text


client = TestClient(app)


def make_pdf(*pages):
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    result = document.tobytes()
    document.close()
    return result


def post_pdf(content):
    return client.post('/parse-payment-order', files={
        'pdf_file': ('payment.pdf', content, 'application/pdf')
    })


def test_parse_payment_order_multiple_pages_and_leading_zeroes():
    response = post_pdf(make_pdf(
        'Declarant Reference\nRegistration Reference\nModel\nAssessed Amount\n'
        '2026 0694NATALIEALLIE\n\n2026 00RB12345\nIM4\n28.72',
        '2026 37241KESHONDALEY\n2026 00RB777\nIM\n21.09\n'
        '2026 002ALICE\n00RB001\nIM7\n1,234.56',
    ))
    assert response.status_code == 200
    assert response.json() == [
        {'declarant_ref': '2026 0694NATALIEALLIE', 'digits': '0694', 'amount_ec': 28.72},
        {'declarant_ref': '2026 37241KESHONDALEY', 'digits': '37241', 'amount_ec': 21.09},
        {'declarant_ref': '2026 002ALICE', 'digits': '002', 'amount_ec': 1234.56},
    ]


def test_incomplete_or_bad_rows_are_skipped_without_borrowing_next_amount():
    text = ('2026 123NOREF\nIM4\n55.00\n'
            '2026 999WRONGMODEL\n00RB123\nOther\n1.00\n'
            '2026 222BADAMOUNT\n00RB234\nIM4\nnot-a-number\n'
            '2026 555VALID\n00RB555\nIM4\n0.00\n'
            '2026 1234\n00RB333\nIM4\n5.00')
    assert parse_payment_order_text(text) == [
        {'declarant_ref': '2026 555VALID', 'digits': '555', 'amount_ec': 0.0}
    ]


def test_empty_and_invalid_pdfs_return_400():
    assert post_pdf(b'').status_code == 400
    assert post_pdf(b'not a pdf').status_code == 400
    assert client.post('/parse-payment-order').status_code == 422


def test_blank_pdf_returns_empty_list():
    assert post_pdf(make_pdf('An unrelated summary')).json() == []
