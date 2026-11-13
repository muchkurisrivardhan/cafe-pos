# Guns & Roses Café POS

Billing, kitchen orders (KOT) and daily reports for dine-in, takeaway and delivery.
One Windows PC at the counter runs the server, and staff use any iPad or Android tablet's browser over the café Wi-Fi.

## Run it
1. Install Python 3 on the counter PC (python.org; tick "Add to PATH").
2. Double-click `start.bat`. It prints the address for tablets, e.g. `http://192.168.1.20:8000`.
3. Windows Firewall will ask about Python. Allow it on **Private** networks.
4. On each tablet, open that address, then use Share → **Add to Home Screen** to get an app icon.
5. The staff PIN is `1234`. **Change it in Settings on day one.** Also fill in your GSTIN, FSSAI licence number and phone there.

All data is in `pos.db`. Copy that file somewhere safe every day.
`menu.csv` only loads on the very first run. After that, edit the menu in the app.

## How it works
| Screen | Who | What |
|---|---|---|
| Tables | Waiters / counter | Tap a table (or + Takeaway / + Delivery), tap items, add a kitchen note, then **Send to kitchen**. |
| Kitchen | Kitchen tablet | Live list of KOTs (refreshes every 5 s). A ticket turns red after 15 min. Tap **Mark ready**. |
| Bill & print | Counter | Pick Cash / UPI / Card, set an optional discount %, and print. GST is split into CGST + SGST and the total is rounded to the nearest ₹1. |
| Reports | Owner | For any date: net sales, GST, discounts, cancelled orders, totals by payment and by order type, item-wise sales, and bill reprints. |

Printing uses the tablet's normal print dialog: AirPrint on iPad, the print service on Android.
Any 80 mm thermal printer that the tablet can see works.

## Development plan
**Phase 1: done (this build)**
- Orders for 8 tables (set the count in Settings), takeaway, and delivery with customer name, phone and address
- KOT flow with a kitchen display, kitchen notes, and "cooking/ready" status on every table
- GST tax invoice: GSTIN, FSSAI, CGST/SGST, round-off, sequential bill numbers
- Daily sales report and reprints
- Staff PIN, and the full café menu loaded from the PDF

**Phase 2: once you've picked your hardware**
- Direct thermal printing (ESC/POS over LAN) so bills and KOTs print without a dialog. Needs the printer model.
- Printed KOT in the kitchen, as an alternative to the kitchen tablet
- Daily automatic backup of `pos.db` (to a USB stick or Google Drive)
- Bill numbers that restart each financial year (e.g. `GR/26-27/0001`)

**Phase 3: after a few weeks of real use**
- Void/refund of printed bills with a reason (an audit trail for the owner)
- Separate PINs for owner and staff (staff can't edit the menu or see reports)
- Month and GST-period reports with CSV export for your accountant
- UPI QR code on the bill, and a packing charge for takeaway/delivery (for now, add it as a menu item)
- Swiggy/Zomato order entry, and a customer phone book for repeat deliveries

## Checks
`python app.py test` runs the GST and rounding maths and a full order → KOT → bill → report flow against a throwaway database.
