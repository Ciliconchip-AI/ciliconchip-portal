"""Payslip PDF for the Ciliconchip Portal (logo top right, like the screening app's reports)."""

from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .payroll import rupees_in_words
from .security import mask

ORANGE = colors.HexColor("#FF6600")
INK = colors.HexColor("#17181C")
LINE = colors.HexColor("#E7E8EC")
SOFT = colors.HexColor("#F7F7F9")
LOGO = Path(__file__).resolve().parent.parent / "assets" / "ciliconchip_logo.png"


def _inr(v) -> str:
    v = int(round(v or 0))
    s = str(abs(v))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if v < 0 else "") + s


def payslip_pdf(slip: dict, emp: dict, company: str, address: str = "") -> bytes:
    buf = BytesIO()
    month = datetime.strptime(slip["month"], "%Y-%m")

    def header(canvas, doc):
        canvas.saveState()
        if LOGO.exists():
            img = ImageReader(str(LOGO))
            iw, ih = img.getSize()
            w = 46 * mm
            h = w * ih / iw
            canvas.drawImage(img, A4[0] - doc.rightMargin - w, A4[1] - 14 * mm - h, w, h, mask="auto")
        canvas.setFont("Helvetica-Bold", 15)
        canvas.setFillColor(INK)
        canvas.drawString(doc.leftMargin, A4[1] - 20 * mm, company or "Ciliconchip")
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.HexColor("#666B73"))
        if address:
            canvas.drawString(doc.leftMargin, A4[1] - 25 * mm, address[:110])
        canvas.setFillColor(ORANGE)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(doc.leftMargin, A4[1] - 32 * mm, f"Payslip for {month:%B %Y}")
        canvas.setStrokeColor(ORANGE)
        canvas.setLineWidth(1.2)
        canvas.line(doc.leftMargin, A4[1] - 35 * mm, A4[0] - doc.rightMargin, A4[1] - 35 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#8A8F98"))
        canvas.drawString(doc.leftMargin, 12 * mm, "This is a system-generated payslip and does not need a signature. "
                                                    "Confidential - for the named employee only.")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=40 * mm,
                            bottomMargin=18 * mm, title=f"Payslip {month:%B %Y}", author=company)
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, leading=11, textColor=INK)
    bold = ParagraphStyle("b", parent=small, fontName="Helvetica-Bold")
    priv = emp.get("private") or {}
    info = [
        ["Employee name", emp.get("full_name", ""), "Employee code", emp.get("emp_code", "")],
        ["Designation", emp.get("designation") or "-", "Department", emp.get("department") or "-"],
        ["Date of joining", f"{emp['date_of_joining']:%d %b %Y}" if emp.get("date_of_joining") else "-",
         "Location", emp.get("location") or "-"],
        ["PAN", mask(priv.get("pan", ""), 4) or "-", "UAN", emp.get("uan") or "-"],
        ["Bank account", mask(priv.get("bank_account", ""), 4) or "-", "Tax regime",
         "New" if slip.get("regime") == "new" else "Old"],
        ["Days in month", f"{slip['days_in_month']}", "Paid days",
         f"{slip['paid_days']:g}" + (f"  (LOP {slip['lwp_days']:g})" if slip.get("lwp_days") else "")],
    ]
    t = Table([[Paragraph(escape(str(c)), bold if i % 2 == 0 else small) for i, c in enumerate(r)] for r in info],
              colWidths=[30 * mm, 59 * mm, 30 * mm, 59 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, LINE), ("BACKGROUND", (0, 0), (0, -1), SOFT),
                           ("BACKGROUND", (2, 0), (2, -1), SOFT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))

    earn = list(slip["earnings"].items())
    ded = list(slip["deductions"].items())
    n = max(len(earn), len(ded))
    rows = [[Paragraph("Earnings", bold), Paragraph("Amount (Rs.)", bold), Paragraph("Deductions", bold),
             Paragraph("Amount (Rs.)", bold)]]
    for i in range(n):
        e = earn[i] if i < len(earn) else ("", "")
        d = ded[i] if i < len(ded) else ("", "")
        rows.append([Paragraph(escape(e[0]), small), Paragraph(_inr(e[1]) if e[1] != "" else "", small),
                     Paragraph(escape(d[0]), small), Paragraph(_inr(d[1]) if d[1] != "" else "", small)])
    rows.append([Paragraph("Gross earnings", bold), Paragraph(_inr(slip["gross"]), bold),
                 Paragraph("Total deductions", bold), Paragraph(_inr(slip["total_deductions"]), bold)])
    pay = Table(rows, colWidths=[59 * mm, 30 * mm, 59 * mm, 30 * mm])
    pay.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, LINE), ("BACKGROUND", (0, 0), (-1, 0), SOFT),
                             ("BACKGROUND", (0, -1), (-1, -1), SOFT), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                             ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    net = Table([[Paragraph("<b>Net pay</b>", ParagraphStyle("n", parent=small, fontSize=11, textColor=colors.white)),
                  Paragraph(f"<b>Rs. {_inr(slip['net'])}</b>",
                            ParagraphStyle("n2", parent=small, fontSize=11, alignment=2, textColor=colors.white))],
                 [Paragraph(escape(rupees_in_words(slip["net"])),
                            ParagraphStyle("w", parent=small, textColor=colors.white)), ""]],
                colWidths=[118 * mm, 60 * mm])
    net.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ORANGE), ("SPAN", (0, 1), (1, 1)),
                             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story = [t, Spacer(1, 6 * mm), pay, Spacer(1, 4 * mm), net, Spacer(1, 5 * mm)]
    extra = []
    if slip.get("employer"):
        extra.append("Employer contributions (not deducted from pay): " +
                     ", ".join(f"{k} Rs. {_inr(v)}" for k, v in slip["employer"].items()))
    tx = slip.get("tax") or {}
    if tx:
        extra.append(f"Income tax: projected annual taxable income Rs. {_inr(tx.get('taxable'))}, projected annual tax "
                     f"Rs. {_inr(tx.get('annual_tax'))}, tax deducted earlier this year Rs. {_inr(tx.get('tds_so_far'))} "
                     f"(tax year {slip.get('fy')}).")
    for line in extra:
        story.append(Paragraph(escape(line), ParagraphStyle("x", parent=small, textColor=colors.HexColor("#555A62"))))
        story.append(Spacer(1, 2 * mm))
    doc.build(story, onFirstPage=header, onLaterPages=header)
    return buf.getvalue()
