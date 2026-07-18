import csv
from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle, Image
from reportlab.lib.units import mm
import os
import webbrowser
import shutil

# Company header for PDFs (editable here)
COMPANY_NAME = "PARTS MAFIA LOGISTICS"
COMPANY_ADDRESS = "123 Auto Street, Mumbai - 400001"
COMPANY_CONTACT = "Phone: +91 98765 43210 | Email: partsmafia@example.com"

def _pdf_header(c, title, width, height):
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width/2, height - 30, COMPANY_NAME)
    c.setFont("Helvetica", 9)
    c.drawCentredString(width/2, height - 46, COMPANY_ADDRESS)
    c.drawCentredString(width/2, height - 60, COMPANY_CONTACT)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(width/2, height - 78, title)
    c.setStrokeColor(colors.grey)
    c.setLineWidth(0.6)
    c.line(30, height - 86, width - 30, height - 86)

def export_items_csv(db, path="items_export.csv"):
    rows = db.list_items()
    keys = ["id","sku","barcode","name","category","unit","cost_price","sell_price","qty","reorder_level","created_at"]
    with open(path,"w",newline="",encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(keys)
        for r in rows:
            writer.writerow([r[k] for k in keys])
    pdf_path = Path(path).with_suffix(".pdf")
    _items_to_pdf(rows, str(pdf_path))
    return path

def _items_to_pdf(rows, out_pdf):
    c = canvas.Canvas(out_pdf, pagesize=A4)
    width, height = A4
    _pdf_header(c, "Inventory Items Report", width, height)
    data = [["S.No","Name","Qty","Cost","Sell","Reorder"]]
    for i, r in enumerate(rows, start=1):
        data.append([str(i), r["name"], f"{r['qty']:.2f}", f"{(r['cost_price'] or 0):.2f}", f"{(r['sell_price'] or 0):.2f}", str(r["reorder_level"] or "")])
    table = Table(data, colWidths=[40,260,60,70,70,70])
    style = TableStyle([
        ("GRID",(0,0),(-1,-1),0.5,colors.black),
        ("BACKGROUND",(0,0),(-1,0),colors.lightgrey),
        ("ALIGN",(2,1),(-1,-1),"RIGHT"),
    ])
    table.setStyle(style)
    w, h = table.wrapOn(c, width-60, height-200)
    table.drawOn(c, 30, height - 120 - h)
    c.showPage()
    c.save()
    try:
        webbrowser.open_new(str(Path(out_pdf).absolute()))
    except:
        pass

def export_profit_report(db, path="profit_report.csv"):
    rows = db.conn.execute("""
        SELECT i.id, i.name, i.cost_price, i.sell_price, SUM(sm.qty) as total_sold
        FROM stock_movements sm
        JOIN items i ON sm.item_id = i.id
        WHERE sm.movement_type='sale'
        GROUP BY i.id
    """).fetchall()
    out = Path(path)
    with out.open("w",newline="",encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name","cost_price","sell_price","total_sold","profit"])
        for r in rows:
            profit = (r["sell_price"] - r["cost_price"]) * (r["total_sold"] or 0)
            writer.writerow([r["name"], r["cost_price"], r["sell_price"], r["total_sold"], profit])
    pdf_path = out.with_suffix(".pdf")
    _profit_to_pdf(rows, str(pdf_path))
    return str(out)

def _profit_to_pdf(rows, out_pdf):
    c = canvas.Canvas(out_pdf, pagesize=A4)
    width, height = A4
    _pdf_header(c, "Profit Report", width, height)
    data = [["Name","Cost","Sell","Sold","Profit"]]
    for r in rows:
        profit = (r["sell_price"] - r["cost_price"]) * (r["total_sold"] or 0)
        data.append([r["name"], f"{r['cost_price'] or 0:.2f}", f"{r['sell_price'] or 0:.2f}", str(r["total_sold"] or 0), f"{profit:.2f}"])
    table = Table(data, colWidths=[200,70,70,70,70])
    style = TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.black),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("ALIGN",(1,1),(-1,-1),"RIGHT")])
    table.setStyle(style)
    w,h = table.wrapOn(c, width-60, height-200)
    table.drawOn(c, 30, height - 120 - h)
    c.showPage()
    c.save()
    try:
        webbrowser.open_new(str(Path(out_pdf).absolute()))
    except:
        pass

def sales_chart(db, path="sales_chart.png"):
    rows = db.conn.execute("SELECT date(created_at) as d, SUM(total) as total FROM sales GROUP BY date(created_at) ORDER BY d").fetchall()
    dates = [r["d"] for r in rows]
    totals = [r["total"] for r in rows]
    out = Path(path)
    if not dates:
        fig,ax = plt.subplots()
        ax.text(0.5,0.5,"No sales data",ha="center",va="center")
        fig.savefig(path)
        plt.close(fig)
        return str(path)
    fig,ax = plt.subplots()
    ax.plot(dates, totals, marker="o")
    ax.set_title("Sales by date")
    fig.autofmt_xdate()
    fig.savefig(path)
    plt.close(fig)
    pdf = out.with_suffix(".pdf")
    _chart_to_pdf(str(out), str(pdf))
    return str(out)

def _chart_to_pdf(img_path, out_pdf):
    c = canvas.Canvas(out_pdf, pagesize=A4)
    width, height = A4
    _pdf_header(c, "Sales Chart", width, height)
    try:
        img = Image(img_path)
        iw, ih = img.wrap(width-60, height-160)
        img.drawOn(c, 30, height - 140 - ih)
    except Exception:
        c.setFont("Helvetica", 10)
        c.drawString(40, height - 120, "Chart generation failed or image missing.")
    c.showPage()
    c.save()
    try:
        webbrowser.open_new(str(Path(out_pdf).absolute()))
    except:
        pass

def export_daily(db, path="daily_report.csv"):
    rows = db.conn.execute("SELECT date(created_at) as d, SUM(total) as total FROM sales GROUP BY date(created_at) ORDER BY d").fetchall()
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date","total"])
        for r in rows:
            w.writerow([r["d"], r["total"]])
    pdf_path = Path(path).with_suffix(".pdf")
    _daily_to_pdf(rows, str(pdf_path))
    return path

def _daily_to_pdf(rows, out_pdf):
    c = canvas.Canvas(out_pdf, pagesize=A4)
    width, height = A4
    _pdf_header(c, "Daily Sales Report", width, height)
    data = [["Date","Total"]]
    for r in rows:
        data.append([r["d"], f"{r['total']:.2f}"])
    table = Table(data, colWidths=[200,200])
    style = TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.black),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("ALIGN", (1,1), (-1,-1), "RIGHT")])
    table.setStyle(style)
    w,h = table.wrapOn(c, width-60, height-200)
    table.drawOn(c, 30, height - 120 - h)
    c.showPage()
    c.save()
    try:
        webbrowser.open_new(str(Path(out_pdf).absolute()))
    except:
        pass

def export_monthly(db, path="monthly_report.csv"):
    rows = db.conn.execute("SELECT strftime('%Y-%m',created_at) as m, SUM(total) as total FROM sales GROUP BY m ORDER BY m").fetchall()
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month","total"])
        for r in rows:
            w.writerow([r["m"], r["total"]])
    pdf_path = Path(path).with_suffix(".pdf")
    _monthly_to_pdf(rows, str(pdf_path))
    return path

def _monthly_to_pdf(rows, out_pdf):
    c = canvas.Canvas(out_pdf, pagesize=A4)
    width, height = A4
    _pdf_header(c, "Monthly Sales Report", width, height)
    data = [["Month","Total"]]
    for r in rows:
        data.append([r["m"], f"{r['total']:.2f}"])
    table = Table(data, colWidths=[200,200])
    style = TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.black),("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("ALIGN", (1,1), (-1,-1), "RIGHT")])
    table.setStyle(style)
    w,h = table.wrapOn(c, width-60, height-200)
    table.drawOn(c, 30, height - 120 - h)
    c.showPage()
    c.save()
    try:
        webbrowser.open_new(str(Path(out_pdf).absolute()))
    except:
        pass

def backup_db(db, dest=None):
    if dest is None:
        dest = f"inventory_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    src = str(db.path)
    shutil.copyfile(src, dest)
    return dest

def generate_invoice_pdf(invoice_no, customer, cart_items, discount=0.0, gst=0.0, path=None):
    """
    cart_items: list of dicts with keys: name, qty, price
    gst and discount are percentages (e.g., 18 for 18%)
    Combined GST (single value) is shown as "GST (xx%) = ₹value"
    Table is bordered (classic invoice look) and rows have increased height.
    """
    if path is None:
        path = f"invoice_{invoice_no}.pdf"
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    _pdf_header(c, f"Invoice: {invoice_no}", width, height)

    # customer + meta
    c.setFont("Helvetica", 9)
    c.drawString(40, height - 100, f"Invoice No: {invoice_no}")
    c.drawString(300, height - 100, f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    c.drawString(40, height - 116, f"Customer: {customer}")

    # table rows
    data = [["S.No", "Item", "Qty", "Rate", "Amount"]]
    total = 0.0
    for idx, it in enumerate(cart_items, start=1):
        qty = float(it.get("qty") or 0)
        rate = float(it.get("price") or 0)
        amount = qty * rate
        total += amount
        data.append([str(idx), it.get("name"), f"{qty:.2f}", f"{rate:.2f}", f"{amount:.2f}"])

    # totals
    discount_amount = (total * discount / 100.0) if discount else 0.0
    after_discount = total - discount_amount
    gst_amount = (after_discount * gst / 100.0) if gst else 0.0
    grand_total = after_discount + gst_amount

    data.append(["", "", "", "Subtotal", f"{total:.2f}"])
    data.append(["", "", "", f"Discount ({discount:.1f}%)", f"-{discount_amount:.2f}"])
    data.append(["", "", "", f"GST ({gst:.1f}%)", f"{gst_amount:.2f}"])
    data.append(["", "", "", "Grand Total", f"{grand_total:.2f}"])

    # create table with borders and increased row height
    col_widths = [40, 300, 50, 80, 80]  # tightened item column spacing
    table = Table(data, colWidths=col_widths, repeatRows=1, rowHeights=20)
    style = TableStyle([
        ("GRID", (0,0), (-1,-1), 0.5, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("ALIGN", (2,1), (-1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("SPAN", (0, len(data)-4), (2, len(data)-4)),
        ("SPAN", (0, len(data)-3), (2, len(data)-3)),
        ("SPAN", (0, len(data)-2), (2, len(data)-2)),
        ("SPAN", (0, len(data)-1), (2, len(data)-1)),
    ])
    table.setStyle(style)

    w, h = table.wrapOn(c, width-60, height-220)
    table.drawOn(c, 30, height - 140 - h)

    # footer note
    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(width/2, 40, "Thank you for shopping with Parts Mafia Logistics")
    c.showPage()
    c.save()
    try:
        webbrowser.open_new(str(Path(path).absolute()))
    except:
        pass
    return path
