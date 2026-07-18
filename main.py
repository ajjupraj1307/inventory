from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import Screen
from kivy.uix.popup import Popup
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.core.window import Window
from kivy.properties import ListProperty, StringProperty
from pathlib import Path
import csv, os, random, webbrowser, shutil
import reports
from db import DB
from datetime import datetime

from kivy.graphics import Color, Rectangle

KV_FILE = "inventory.kv"

db = DB()

# optional notification (plyer). If not installed, ignore notifications.
try:
    from plyer import notification
    PLYER_AVAILABLE = True
except Exception:
    PLYER_AVAILABLE = False


class LoginScreen(Screen):
    def do_login(self):
        username = self.ids.username.text.strip()
        password = self.ids.password.text.strip()
        ok, row = db.verify_user(username, password)
        if ok:
            app = App.get_running_app()
            app.user = username
            app.user_row = row
            app.user_role = row["role"]
            self.manager.current = "inventory"
            self.ids.error.text = ""
        else:
            self.ids.error.text = "Invalid credentials"


class InventoryScreen(Screen):
    def on_enter(self):
        self.refresh_items()
        self.check_low_stock()
        self.adjust_ui_by_role()

    def adjust_ui_by_role(self):
        app = App.get_running_app()
        role = getattr(app, "user_role", "staff")
        try:
            if role != "admin":
                self.ids.btn_export_csv.disabled = True
                self.ids.btn_profit.disabled = True
                self.ids.btn_backup.disabled = True
                self.ids.btn_export_daily.disabled = True
                self.ids.btn_export_monthly.disabled = True
                self.ids.btn_chart.disabled = True
            else:
                self.ids.btn_export_csv.disabled = False
                self.ids.btn_profit.disabled = False
                self.ids.btn_backup.disabled = False
                self.ids.btn_export_daily.disabled = False
                self.ids.btn_export_monthly.disabled = False
                self.ids.btn_chart.disabled = False
        except Exception:
            pass

    def refresh_items(self, search_text=""):
        container = self.ids.item_list
        container.clear_widgets()

        # Sort: out-of-stock first, then low-stock, then others
        rows = sorted(
            db.list_items(search_text),
            key=lambda r: (r['qty'] == 0, r['qty'] < r['reorder_level']),
            reverse=True
        )

        if not rows:
            container.add_widget(Label(text="No items found", size_hint_y=None, height=30))
            return

        for idx, r in enumerate(rows, start=1):
            box = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=6, padding=[6, 4])

            lbl_serial = Label(text=f"{idx}.", size_hint_x=None, width=28, halign="left", valign="middle")
            lbl_serial.text_size = (lbl_serial.width, lbl_serial.height)

            name_lbl = Label(text=f"{r['name']}", halign="left", valign="middle")
            qty_lbl = Label(text=f"{r['qty']:.2f}", size_hint_x=None, width=60, halign="right", valign="middle")

            # Apply color based on stock level
            if r["qty"] == 0:
                name_lbl.color = (1, 0, 0, 1)  # Red
                qty_lbl.color = (1, 0, 0, 1)
            elif r["qty"] < r["reorder_level"]:
                name_lbl.color = (1, 1, 0, 1)  # Yellow
                qty_lbl.color = (1, 1, 0, 1)
            else:
                name_lbl.color = (1, 1, 1, 1)  # White/normal

            name_lbl.text_size = (None, None)
            qty_lbl.text_size = (qty_lbl.width, qty_lbl.height)

            btn_addcart = Button(text="Add", size_hint_x=None, width=60)

            def _add(instance, item=r):
                app = App.get_running_app()
                inv = app.root.get_screen("invoice")
                inv.add_to_cart(item["id"], item["name"], item["sell_price"])
                app.root.current = "invoice"

            btn_addcart.bind(on_release=_add)

            btn_edit = Button(text="Edit", size_hint_x=None, width=60)

            def _edit(instance, item=r):
                app = App.get_running_app()
                if getattr(app, "user_role", "staff") != "admin":
                    popup = Popup(title="Permission denied",
                                  content=Label(text="You are not allowed to edit items."),
                                  size_hint=(0.6, 0.4))
                    popup.open()
                    return
                self.edit_item_popup(item)

            btn_edit.bind(on_release=_edit)

            btn_del = Button(text="Del", size_hint_x=None, width=60)

            def _del(instance, item=r):
                app = App.get_running_app()
                if getattr(app, "user_role", "staff") != "admin":
                    popup = Popup(title="Permission denied",
                                  content=Label(text="You are not allowed to delete items."),
                                  size_hint=(0.6, 0.4))
                    popup.open()
                    return
                db.delete_item(item["id"])
                self.refresh_items(self.ids.search_input.text if hasattr(self.ids, "search_input") else "")

            btn_del.bind(on_release=_del)

            box.add_widget(lbl_serial)
            box.add_widget(name_lbl)
            box.add_widget(qty_lbl)
            box.add_widget(btn_addcart)
            box.add_widget(btn_edit)
            box.add_widget(btn_del)
            container.add_widget(box)

    def add_item_popup(self):
        box = BoxLayout(orientation="vertical", spacing=8, padding=8)
        box.add_widget(Label(text="Name"))
        name = TextInput(hint_text="Name", multiline=False)
        box.add_widget(name)
        box.add_widget(Label(text="Qty (pcs)"))
        qty = TextInput(hint_text="Qty", input_filter="float", multiline=False)
        box.add_widget(qty)
        box.add_widget(Label(text="Cost Price (₹)"))
        cost_price = TextInput(hint_text="Cost Price", input_filter="float", multiline=False)
        box.add_widget(cost_price)
        box.add_widget(Label(text="Sell Price (₹)"))
        price = TextInput(hint_text="Sell Price", input_filter="float", multiline=False)
        box.add_widget(price)
        box.add_widget(Label(text="Reorder Level (pcs)"))
        reorder = TextInput(hint_text="Reorder Level", input_filter="int", multiline=False)
        box.add_widget(reorder)
        btn = Button(text="Save", size_hint_y=None, height=44)

        def save_item(instance):
            n = name.text.strip()
            q = float(qty.text.strip()) if qty.text.strip() else 0.0
            cp = float(cost_price.text.strip()) if cost_price.text.strip() else 0.0
            sp = float(price.text.strip()) if price.text.strip() else 0.0
            rl = int(reorder.text.strip()) if reorder.text.strip() else 0
            if n:
                db.add_item(n, qty=q, cost_price=cp, sell_price=sp, reorder_level=rl)
                popup.dismiss()
                self.refresh_items()

        btn.bind(on_release=save_item)
        box.add_widget(btn)
        popup = Popup(title="Add Item", content=box, size_hint=(0.9, 0.85))
        popup.open()

    def edit_item_popup(self, item):
        box = BoxLayout(orientation="vertical", spacing=6, padding=8)
        box.add_widget(Label(text="Name"))
        name = TextInput(text=item["name"] or "", multiline=False)
        box.add_widget(name)

        box.add_widget(Label(text="Category"))
        category = TextInput(text=item["category"] or "", multiline=False)
        box.add_widget(category)

        box.add_widget(Label(text="Unit"))
        unit = TextInput(text=item["unit"] or "", multiline=False)
        box.add_widget(unit)

        box.add_widget(Label(text="Quantity (pcs)"))
        qty = TextInput(text=str(item["qty"] or 0), input_filter="float", multiline=False)
        box.add_widget(qty)

        box.add_widget(Label(text="Cost Price (₹)"))
        cost_price = TextInput(text=str(item["cost_price"] or ""), input_filter="float", multiline=False)
        box.add_widget(cost_price)

        box.add_widget(Label(text="Sell Price (₹)"))
        price = TextInput(text=str(item["sell_price"] or ""), input_filter="float", multiline=False)
        box.add_widget(price)

        box.add_widget(Label(text="Reorder Level (pcs)"))
        reorder = TextInput(text=str(item["reorder_level"] or 0), input_filter="int", multiline=False)
        box.add_widget(reorder)

        btn = Button(text="Update", size_hint_y=None, height=44)

        def do_update(instance):
            try:
                db.update_item(item["id"],
                               qty=float(qty.text.strip() or 0),
                               cost_price=float(cost_price.text.strip() or 0),
                               sell_price=float(price.text.strip() or 0),
                               category=category.text.strip() or None,
                               unit=unit.text.strip() or None,
                               reorder_level=int(reorder.text.strip() or 0))
            except Exception as e:
                print("Update failed:", e)
            popup.dismiss()
            self.refresh_items(self.ids.search_input.text if hasattr(self.ids, "search_input") else "")

        btn.bind(on_release=do_update)
        box.add_widget(btn)
        popup = Popup(title="Edit Item (values shown)", content=box, size_hint=(0.95, 0.95))
        popup.open()

    # ---------- EXPORT / REPORT / BACKUP FUNCTIONS BELOW ----------
    def export_csv(self):
        app = App.get_running_app()
        if getattr(app, "user_role", "staff") != "admin":
            popup = Popup(title="Permission denied", content=Label(text="You are not allowed to export reports."),
                          size_hint=(0.6, 0.4))
            popup.open()
            return
        out = reports.export_items_csv(db, path="items_export.csv")
        try:
            webbrowser.open_new(out)
        except:
            pass

    def export_profit(self):
        app = App.get_running_app()
        if getattr(app, "user_role", "staff") != "admin":
            popup = Popup(title="Permission denied", content=Label(text="You are not allowed to export reports."),
                          size_hint=(0.6, 0.4))
            popup.open()
            return
        out = reports.export_profit_report(db, path="profit_report.csv")
        try:
            webbrowser.open_new(out)
        except:
            pass

    def show_sales_chart(self):
        out = reports.sales_chart(db, path="sales_chart.png")
        try:
            webbrowser.open_new(out)
        except:
            pass

    def backup_db(self):
        app = App.get_running_app()
        if getattr(app, "user_role", "staff") != "admin":
            popup = Popup(title="Permission denied", content=Label(text="You are not allowed to backup the database."),
                          size_hint=(0.6, 0.4))
            popup.open()
            return
        out = reports.backup_db(db)
        try:
            webbrowser.open_new(out)
        except:
            pass

    def export_daily(self):
        app = App.get_running_app()
        if getattr(app, "user_role", "staff") != "admin":
            popup = Popup(title="Permission denied", content=Label(text="You are not allowed to export reports."),
                          size_hint=(0.6, 0.4))
            popup.open()
            return
        out = reports.export_daily(db, path="daily_report.csv")
        try:
            webbrowser.open_new(out)
        except:
            pass

    def export_monthly(self):
        app = App.get_running_app()
        if getattr(app, "user_role", "staff") != "admin":
            popup = Popup(title="Permission denied", content=Label(text="You are not allowed to export reports."),
                          size_hint=(0.6, 0.4))
            popup.open()
            return
        out = reports.export_monthly(db, path="monthly_report.csv")
        try:
            webbrowser.open_new(out)
        except:
            pass

    def check_low_stock(self):
        low = db.get_low_stock_items()
        if not low:
            return
        content = BoxLayout(orientation="vertical", spacing=8, padding=8)
        for r in low:
            content.add_widget(Label(text=f"{r['name']} — Qty: {r['qty']:.2f}  Reorder: {r['reorder_level']}", font_size=15))
        btn = Button(text="Close", size_hint_y=None, height=44)
        content.add_widget(btn)
        popup = Popup(title="Low stock alert", content=content, size_hint=(0.85, 0.6))
        btn.bind(on_release=popup.dismiss)
        popup.open()
        if PLYER_AVAILABLE:
            try:
                notification.notify(title="Low stock items", message=f"{len(low)} items low in stock", timeout=5)
            except:
                pass


class InvoiceScreen(Screen):
    cart = ListProperty([])

    def on_enter(self):
        try:
            self.ids.discount_input.bind(text=lambda *a: self.update_totals())
            self.ids.gst_input.bind(text=lambda *a: self.update_totals())
        except Exception:
            pass
        self.refresh_cart()

    def add_to_cart(self, item_id, name, price):
        for it in self.cart:
            if it["id"] == item_id:
                it["qty"] += 1
                self.refresh_cart()
                self.update_totals()
                return
        self.cart.append({"id": item_id, "name": name, "price": price, "qty": 1})
        self.refresh_cart()
        self.update_totals()

    def refresh_cart(self):
        container = self.ids.cart_list
        container.clear_widgets()
        for it in self.cart:
            box = BoxLayout(size_hint_y=None, height=40, spacing=6, padding=[6, 4])
            lbl = Label(text=f"{it['name']}  x{it['qty']}  @ {it['price']:.2f}", halign="left", valign="middle")
            lbl.text_size = (lbl.width, lbl.height)
            btn_plus = Button(text="+", size_hint_x=None, width=44)
            btn_minus = Button(text="-", size_hint_x=None, width=44)

            def plus(instance, it=it):
                it["qty"] += 1
                self.refresh_cart()
                self.update_totals()

            def minus(instance, it=it):
                it["qty"] = max(0, it["qty"] - 1)
                if it["qty"] == 0:
                    self.cart.remove(it)
                self.refresh_cart()
                self.update_totals()

            btn_plus.bind(on_release=plus)
            btn_minus.bind(on_release=minus)
            box.add_widget(lbl)
            box.add_widget(btn_plus)
            box.add_widget(btn_minus)
            container.add_widget(box)

        self.update_totals()

    def update_totals(self):
        try:
            total = sum(float(it['qty']) * float(it['price']) for it in self.cart)
            discount = float(self.ids.discount_input.text or 0)
            gst = float(self.ids.gst_input.text or 0)
            discounted = total - (total * discount / 100.0)
            gst_amount = discounted * gst / 100.0
            net_total = discounted + gst_amount
            self.ids.total_lbl.text = f"Total: ₹{total:.2f}"
            self.ids.net_total_lbl.text = f"Net Total: ₹{net_total:.2f}"
        except Exception:
            self.ids.total_lbl.text = f"Total: ₹{0.00:.2f}"
            self.ids.net_total_lbl.text = f"Net Total: ₹{0.00:.2f}"

    def checkout(self):
        if not self.cart:
            popup = Popup(title="Empty cart", content=Label(text="Cart is empty"), size_hint=(0.6, 0.4))
            popup.open()
            return
        customer = self.ids.customer.text.strip() or "Walk-in"
        total = 0.0
        for it in self.cart:
            total += it["price"] * it["qty"]
            db.add_stock_movement(it["id"], it["qty"], "sale", note=f"Sold to {customer}")
        invoice_no = f"INV{random.randint(1000, 9999)}"
        db.add_sale(invoice_no, customer, total)
        try:
            disc = float(self.ids.discount_input.text or 0)
        except:
            disc = 0.0
        try:
            gst = float(self.ids.gst_input.text or 0)
        except:
            gst = 0.0
        cart_items = [{"name": it["name"], "qty": it["qty"], "price": it["price"]} for it in self.cart]
        pdf_path = reports.generate_invoice_pdf(invoice_no, customer, cart_items, discount=disc, gst=gst,
                                                path=f"invoice_{invoice_no}.pdf")
        self.cart = []
        self.refresh_cart()
        try:
            webbrowser.open_new(pdf_path)
        except:
            pass


class InventoryApp(App):
    user = StringProperty(None)
    user_role = StringProperty("staff")

    def build(self):
        return Builder.load_file(KV_FILE)

    def on_stop(self):
        try:
            backups_dir = Path("backups")
            backups_dir.mkdir(exist_ok=True)
            dest = backups_dir / f"inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copyfile(str(db.path), str(dest))
            print(f"[backup] saved to {dest}")
        except Exception as e:
            print("Backup failed on exit:", e)


if __name__ == "__main__":
    InventoryApp().run()
