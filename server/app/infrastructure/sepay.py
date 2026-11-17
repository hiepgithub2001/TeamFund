"""PaymentQr port: SePay's VietQR image URL."""
from urllib.parse import quote


class SepayQr:
    def url(self, bank_name: str, bank_account: str, amount: int, content: str) -> str:
        return ("https://qr.sepay.vn/img?acc={acc}&bank={bank}&amount={amount}&des={des}&template=compact"
                .format(acc=quote(bank_account or ""), bank=quote(bank_name or ""), amount=amount, des=quote(content)))

