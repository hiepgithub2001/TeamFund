"""SpreadsheetWriter port: Excel files with openpyxl."""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font


class XlsxWriter:
    def workbook(self, sheets: list[tuple[str, list[str], list[list]]]) -> bytes:
        wb = Workbook()
        wb.remove(wb.active)
        for title, header, rows in sheets:
            ws = wb.create_sheet(title)
            ws.append(header)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for row in rows:
                ws.append(row)
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 50)
        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
