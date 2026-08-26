"""
PDF-Steuerreport
================

Baut den Report als echtes PDF (nicht mehr per window.print()-Umweg):
Seite 1 ist ein Deckblatt mit dem Ergebnis je Steuertopf, danach folgen die
einzelnen Buchungen gruppiert nach Topf (Paragraph 23 / 20 / 22).

reportlab ist reine Bibliothek, kein Systemabhaengigkeit wie bei
weasyprint - laeuft ueberall dort, wo auch python3 selbst laeuft.
"""

from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable
)
from reportlab.pdfbase.pdfmetrics import stringWidth

# Dieselben Farben wie im Dashboard, damit ein Topf auf jeder Seite
# wiederzuerkennen ist.
COL = {
    "par23": colors.HexColor("#1F9D74"),
    "par20": colors.HexColor("#3D6FD1"),
    "par22": colors.HexColor("#B3811F"),
}
INK = colors.HexColor("#151A2B")
DIM = colors.HexColor("#6B7484")
LINE = colors.HexColor("#E1E5EC")
BG = colors.HexColor("#F6F7FA")
DOWN = colors.HexColor("#C0392B")


def _money(v):
    if v in (None, ""):
        return "–"
    v = Decimal(str(v))
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def _day(s):
    if not s:
        return "–"
    y, m, d = s[:10].split("-")
    return f"{d}.{m}.{y}"


def _styles():
    ss = getSampleStyleSheet()
    return {
        "eyebrow": ParagraphStyle("eyebrow", parent=ss["Normal"], fontName="Helvetica-Bold",
                                  fontSize=8.5, textColor=DIM, tracking=1, spaceAfter=4),
        "title": ParagraphStyle("title", parent=ss["Normal"], fontName="Helvetica-Bold",
                                fontSize=22, textColor=INK, spaceAfter=2, leading=26),
        "sub": ParagraphStyle("sub", parent=ss["Normal"], fontName="Helvetica",
                              fontSize=9.5, textColor=DIM, spaceAfter=18),
        "h2": ParagraphStyle("h2", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=12.5, textColor=INK, spaceBefore=4, spaceAfter=8),
        "potlaw": ParagraphStyle("potlaw", parent=ss["Normal"], fontName="Helvetica-Bold",
                                 fontSize=8, textColor=DIM),
        "potname": ParagraphStyle("potname", parent=ss["Normal"], fontName="Helvetica-Bold",
                                  fontSize=11, textColor=INK, spaceAfter=2),
        "potmeta": ParagraphStyle("potmeta", parent=ss["Normal"], fontName="Helvetica",
                                  fontSize=8.5, textColor=DIM),
        "potval": ParagraphStyle("potval", parent=ss["Normal"], fontName="Helvetica-Bold",
                                 fontSize=15, textColor=INK, alignment=2),
        "note": ParagraphStyle("note", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=8, textColor=DIM, leading=11),
        "warn": ParagraphStyle("warn", parent=ss["Normal"], fontName="Helvetica-Bold",
                               fontSize=9, textColor=DOWN, leading=12),
        "cell": ParagraphStyle("cell", parent=ss["Normal"], fontName="Helvetica",
                               fontSize=8.7, textColor=INK, leading=11),
        "cellDim": ParagraphStyle("cellDim", parent=ss["Normal"], fontName="Helvetica",
                                  fontSize=7.5, textColor=DIM, leading=10),
        "cellNum": ParagraphStyle("cellNum", parent=ss["Normal"], fontName="Helvetica-Bold",
                                  fontSize=8.7, textColor=INK, alignment=2),
        "groupHead": ParagraphStyle("groupHead", parent=ss["Normal"], fontName="Helvetica-Bold",
                                    fontSize=11, textColor=colors.white),
    }


def _pot_table(styles, law, name, meta, value, status_text, status_ok, bucket_key):
    bar = Table([[""]], colWidths=[5 * mm], rowHeights=[20 * mm])
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COL[bucket_key]),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    body = Table([
        [Paragraph(law, styles["potlaw"]), ""],
        [Paragraph(name, styles["potname"]), Paragraph(_money(value), styles["potval"])],
        [Paragraph(meta, styles["potmeta"]),
         Paragraph(status_text, ParagraphStyle(
             "st", parent=styles["potmeta"], alignment=2, textColor=DIM,
             fontName="Helvetica-Bold"))],
    ], colWidths=[95 * mm, 50 * mm])
    body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("SPAN", (0, 0), (1, 0)),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 2),
    ]))

    outer = Table([[bar, body]], colWidths=[5 * mm, 165 * mm])
    outer.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (1, 0), (1, 0), BG),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
        ("RIGHTPADDING", (1, 0), (1, 0), 8),
        ("TOPPADDING", (1, 0), (1, 0), 9),
        ("BOTTOMPADDING", (1, 0), (1, 0), 9),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
    ]))
    return outer


def _cover(report, year, meta_text, styles):
    y = next(v for v in report["years"] if v["year"] == year)
    flow = [
        Paragraph("STEUERREPORT · DECKBLATT", styles["eyebrow"]),
        Paragraph(f"Jahresergebnis {year}", styles["title"]),
        Paragraph(meta_text, styles["sub"]),
    ]

    blockers = []
    if report.get("unknownCount"):
        blockers.append(f"{report['unknownCount']} Vorgänge sind keinem Steuertopf zugeordnet.")
    if report.get("missingRatesCount"):
        blockers.append(f"Für {report['missingRatesCount']} Positionen fehlt ein EUR-Kurs.")
    if blockers:
        flow.append(Paragraph(
            "REPORT NOCH NICHT BELASTBAR: " + " ".join(blockers), styles["warn"]))
        flow.append(Spacer(1, 10))

    p23, p20, p22 = y["par23"], y["par20"], y["par22"]
    flow.append(_pot_table(
        styles, "§ 23 EStG", "Private Veräußerungsgeschäfte",
        f"{p23['count']} steuerpflichtig · {p23['countTaxFree']} steuerfrei"
        + (f" · Verlustvortrag {_money(p23['lossCarryIn'])}" if p23["lossCarryIn"] else ""),
        p23["taxable"],
        "unter Freigrenze" if not p23["exceeded"] else "Freigrenze überschritten",
        not p23["exceeded"], "par23"))
    flow.append(Spacer(1, 8))
    flow.append(_pot_table(
        styles, "§ 20 EStG · Anlage KAP", "Termingeschäfte",
        f"{p20['count']} Buchungen"
        + (f" · Verlustvortrag {_money(p20['lossCarryIn'])}" if p20["lossCarryIn"] else ""),
        p20["resultAfterCarry"], "Sparerpauschbetrag beachten", True, "par20"))
    flow.append(Spacer(1, 8))
    flow.append(_pot_table(
        styles, "§ 22 Nr. 3 EStG", "Sonstige Einkünfte",
        f"{p22['count']} Buchungen"
        + (f" · Verlustvortrag {_money(p22['lossCarryIn'])}" if p22["lossCarryIn"] else ""),
        p22["taxable"],
        "unter Freigrenze" if not p22["exceeded"] else "Freigrenze überschritten",
        not p22["exceeded"], "par22"))

    flow.append(Spacer(1, 16))
    flow.append(HRFlowable(width="100%", color=LINE, thickness=0.7))
    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "Die Zuordnung zu den Steuertöpfen bildet eine Auffassung ab, keine gesicherte "
        "Rechtslage – insbesondere die Einordnung von Krypto-Perpetuals als Termingeschäft "
        "nach § 20 EStG ist nicht abschließend geklärt. Vor Verwendung in einer "
        "Steuererklärung fachlich prüfen lassen. Dies ist keine Steuerberatung.",
        styles["note"]))
    return flow


def _group_header(styles, bucket_key, title, count):
    t = Table([[Paragraph(title, styles["groupHead"]),
                Paragraph(f"{count} Vorgänge",
                         ParagraphStyle("c", parent=styles["groupHead"], alignment=2,
                                       fontName="Helvetica", fontSize=8.5))]],
              colWidths=[133 * mm, 37 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COL[bucket_key]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 10),
        ("RIGHTPADDING", (1, 0), (1, 0), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def _rows_table(styles, rows_data):
    """rows_data: list of (date, desc, sub, value_str, taxable_bool_or_None)"""
    header = [Paragraph("Datum", styles["cellDim"]), Paragraph("Vorgang", styles["cellDim"]),
              Paragraph("Betrag", ParagraphStyle("h", parent=styles["cellDim"], alignment=2))]
    data = [header]
    for date, desc, sub, val in rows_data:
        cell = Paragraph(f"{desc}<br/><font color='#6B7484' size=7>{sub}</font>"
                         if sub else desc, styles["cell"])
        data.append([Paragraph(date, styles["cellDim"]), cell,
                     Paragraph(val, styles["cellNum"])])
    t = Table(data, colWidths=[18 * mm, 118 * mm, 34 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _par23_rows(disposals):
    out = []
    for d in disposals:
        sub = f"{d['held_days']} Tage gehalten · " + \
              ("steuerpflichtig" if d["taxable"] else "steuerfrei")
        out.append((_day(d["sold"]), f"{d['qty']} {d['asset']} verkauft", sub,
                    _money(d["gain_eur"])))
    return out


def _flat_rows(rows):
    out = []
    for r in rows:
        desc = r["note"] or r["txType"]
        out.append((_day(r["date"]), desc, "", _money(r["eur"])))
    return out


def build_pdf(report, year, meta_text) -> bytes:
    styles = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=16 * mm,
                            title=f"Steuerreport {year}")

    flow = _cover(report, year, meta_text, styles)
    flow.append(PageBreak())

    y = next(v for v in report["years"] if v["year"] == year)
    sections = [
        ("par23", "§ 23 — Private Veräußerungsgeschäfte",
         _par23_rows(y["par23"]["disposals"]), y["par23"]["count"] + y["par23"]["countTaxFree"]),
        ("par20", "§ 20 — Termingeschäfte (Futures)",
         _flat_rows(y["par20"]["rows"]), y["par20"]["count"]),
        ("par22", "§ 22 — Sonstige Einkünfte",
         _flat_rows(y["par22"]["rows"]), y["par22"]["count"]),
    ]

    flow.append(Paragraph(f"Buchungen {year}, gruppiert nach Steuertopf", styles["h2"]))
    flow.append(Spacer(1, 4))

    for key, title, rows, count in sections:
        flow.append(_group_header(styles, key, title, count))
        if rows:
            flow.append(_rows_table(styles, rows))
        else:
            flow.append(Spacer(1, 4))
            flow.append(Paragraph("Keine Buchungen in diesem Jahr.", styles["note"]))
        flow.append(Spacer(1, 14))

    doc.build(flow)
    return buf.getvalue()
