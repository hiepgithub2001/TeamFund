from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bill, Expense, Member, Payment, Team
from .timeutil import period_label, to_local_str


def _sheet(wb: Workbook, title: str, header: list[str], rows: list[list]):
    ws = wb.create_sheet(title)
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 50)


def export_team(db: Session, team: Team) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    members = db.scalars(select(Member).where(Member.team_id == team.id).order_by(Member.name)).all()
    names = {m.id: m.name for m in members}
    _sheet(wb, "Members", ["ID", "Name", "Nickname", "Shirt", "Phone", "Note", "Custom fee", "Status"],
           [[m.id, m.name, m.nickname, m.shirt_number, m.phone, m.note, m.fee_override, m.status]
            for m in members])
    bills = db.scalars(select(Bill).where(Bill.team_id == team.id).order_by(Bill.period, Bill.id)).all()
    _sheet(wb, "Bills", ["ID", "Member", "Type", "Title", "Month", "Amount", "Status", "Paid at"],
           [[b.id, names.get(b.member_id), b.kind, b.title, period_label(b.period), b.amount, b.status,
             to_local_str(b.paid_at)] for b in bills])
    pays = db.scalars(select(Payment).where(Payment.team_id == team.id).order_by(Payment.created_at)).all()
    _sheet(wb, "Payments", ["ID", "Time", "Source", "Amount", "Status", "For", "Bank content"],
           [[p.id, to_local_str(p.created_at), p.source, p.amount, p.status, p.note, p.content] for p in pays])
    exps = db.scalars(select(Expense).where(Expense.team_id == team.id).order_by(Expense.created_at)).all()
    _sheet(wb, "Expenses", ["ID", "Time", "Amount", "Title"],
           [[e.id, to_local_str(e.created_at), e.amount, e.title] for e in exps])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
