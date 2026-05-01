from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_submit_endpoint_returns_thank_you_redirect():
    response = client.post(
        "/api/consultant-demo/soberstack/submit",
        json={
            "merchant_name": "Blue Bottle Coffee",
            "service_day": "2026-04-21",
            "receipt_gross": "13.53",
            "claim_bucket": "Food",
            "_derived_claim_amount": "12.00",
            "_derived_explanation": "Removed a non-reimbursable snack.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "/consultant-demo/soberstack/thank-you" in body["redirect_url"]
    assert "submission_id=" in body["redirect_url"]
    assert "claimed_amount=12.00" in body["redirect_url"]


def test_thank_you_page_renders_submission_details():
    response = client.get(
        "/consultant-demo/china/thank-you",
        params={
            "submission_id": "CHI-20260421-120000",
            "claimed_amount": "88.00",
            "receipt_total": "100.00",
            "vendor": "上海餐厅",
            "expense_date": "2026-04-21",
            "expense_category": "餐饮",
            "explanation": "扣除个人消费部分。",
            "submitted_at": "2026-04-21 12:00:00 UTC",
        },
    )

    assert response.status_code == 200
    assert "报销申请已成功提交" in response.text
    assert "CHI-20260421-120000" in response.text
    assert "上海餐厅" in response.text
