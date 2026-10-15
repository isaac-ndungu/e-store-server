"""Canonical storefront texts for the content pages.

Single source of truth for the published legal and shop pages. The
``seed_policy_pages`` management command publishes these bodies to the
``ContentPage`` rows the storefront renders; staff may edit the rows
afterwards, and re-running the command without ``--update`` never
overwrites staff edits.

``POLICY_VERSION`` must be bumped on every substantive wording change.
The texts below use
``[BRACKETED]`` placeholders for facts only the business can supply
(legal entity name, addresses, vendor names); every placeholder must be
replaced before publishing.

These drafts were written against the actual data flows in this codebase
(Pixel/CAPI events, hand-off inquiries, mailto contact form, cookie cart,
7-day cooling-off window, per-product warranty fields, county delivery
areas, manual refunds) and follow the Kenya Data Protection Act, 2019
for the privacy material. They are not legal
advice: a Kenyan data protection lawyer must review them before they
go live, especially the lawful bases, cross-border transfer wording,
retention periods, and ODPC registration status.
"""

POLICY_VERSION = "1.2"

POLICY_PAGES = [
    {
        "slug": "privacy-policy",
        "title": "Privacy Policy",
        "meta_title": "Privacy Policy | Vifaar Appliances",
        "meta_description": (
            "How Vifaar Appliances collects, uses, shares, and protects "
            "your personal data, and the rights you have under the Kenya "
            "Data Protection Act, 2019."
        ),
        "body": """<p><strong>Effective date: 13 October 2026 (version 1.1).</strong></p>
<p>Vifaar Appliances ("Vifaar", "we", "us") runs this online store from New Al Noor Exhibitions building along Taveta Road, Nairobi, Kenya. This policy explains what personal data we collect when you use our website, why we collect it, who receives it, how long we keep it, and the rights you have under the Kenya Data Protection Act, 2019 ("the Act").</p>
<p>Privacy queries, requests, and complaints: <strong>vifaarkenya@gmail.com</strong>. We have not appointed a separate data protection officer, so please direct all privacy matters to that address. You also have the right to complain to the Office of the Data Protection Commissioner (ODPC).</p>
<h2>1. What we collect</h2>
<p><strong>Identity and contact data you give us.</strong> When you check out through WhatsApp or email, or write to us, you share your name, phone number, email address, delivery location, and any order notes you add. When you use the contact form, the message opens in your own email app addressed to us; our website does not store it.</p>
<p><strong>Order data.</strong> The items in your cart, quantities, prices shown at the time, your chosen delivery area, coupon use, and the channel you checked out through (WhatsApp or email), plus the reference we issue for email orders.</p>
<p><strong>Notification records.</strong> When we text or email you about an order, we keep an audit log of the recipient, the message, and whether it was delivered, so we can prove what was sent. If you agree to marketing messages, that agreement and any opt-out are recorded the same way.</p>
<p><strong>Device and browsing data.</strong> Pages you visit, products you view, searches you run, items you save to your wishlist or add to your cart, and technical identifiers such as your IP address, browser type, and advertising cookies described in our Cookie Policy.</p>
<p><strong>Marketing preferences.</strong> Whether you accepted or declined advertising measurement, and when you made that choice.</p>
<h2>2. Why we use it, and the legal basis for each purpose</h2>
<p><strong>To fulfil your order (contract).</strong> Recording your inquiry, confirming payment and delivery with you, arranging delivery and installation, and providing warranty and after-sales support. We do not need separate consent to do what you asked us to do.</p>
<p><strong>To measure advertising (consent).</strong> Advertising measurement only ever runs where you have consented to it. Withdrawing consent stops further measurement immediately; data already sent to Meta stays subject to Meta's policy. See our Cookie Policy for exactly what is sent to Meta.</p>
<p><strong>To send marketing you agreed to (consent).</strong> Promotional SMS or email only goes to customers who opted in. Every message tells you how to stop, and you can also object at any time through vifaarkenya@gmail.com; we keep a suppression record so you stay opted out.</p>
<p><strong>To keep the service safe (legitimate interests).</strong> Rate limiting, fraud prevention, and security logging, kept proportionate to the threat and never used for marketing.</p>
<p><strong>To meet legal obligations.</strong> Tax invoices, accounting records, and any disclosures the law requires (for example to the Kenya Revenue Authority).</p>
<h2>3. Who receives your data</h2>
<p><strong>Meta Platforms</strong> (advertising measurement, including hashed contact details, IP address, browser type, and advertising cookies; data is processed outside Kenya - see section 4). <strong>Our hosting and infrastructure providers</strong> (website hosting on Render, PostgreSQL database on Neon, Redis caching, product imagery on Cloudinary). <strong>Resend</strong> (order confirmation emails). <strong>Africa's Talking</strong> (transactional SMS, for example order updates). <strong>Couriers and installers</strong> (our contracted courier, ParcelGrid Escrow Service, and installation technicians receive your name, phone, and delivery location - only what a delivery needs). <strong>Payment partners</strong> (M-Pesa, bank transfer, or cash on delivery - payment details are confirmed with you directly and are never entered on this website). A current vendor list is available on request from vifaarkenya@gmail.com.</p>
<h2>4. International transfers</h2>
<p>Meta, Resend, Cloudinary, Neon, and some of our other cloud providers process data outside Kenya, including in the United States and (for Meta) Ireland. Where the Act requires safeguards for cross-border transfers, we rely on the transfer mechanisms available to us and keep transfers to what each purpose needs. Ask vifaarkenya@gmail.com for details of the current safeguards.</p>
<h2>5. How long we keep data</h2>
<p>Order inquiries are kept for up to 180 days and then deleted or anonymised. Consent records stay on your own device until you clear them. Tax and accounting records are kept as long as the law requires. Marketing measurement data held by Meta follows Meta's own retention. When retention ends, data is deleted or irreversibly anonymised.</p>
<h2>6. Your rights</h2>
<p>Under the Act you may: ask what we hold about you (access); ask us to fix mistakes (correction); ask us to delete data we no longer need (erasure); object to direct marketing and to processing based on our legitimate interests; withdraw tracking consent at any time; ask for a portable copy of data you gave us; and complain to the ODPC. Send requests to vifaarkenya@gmail.com with proof of identity; we respond within the statutory timelines.</p>
<h2>7. Security</h2>
<p>We protect data with encrypted connections, access controls limited to staff who need them, and minimisation (we collect only what each purpose needs). No system is perfectly secure; if a breach affects you, we will notify you and the ODPC as the law requires.</p>
<h2>8. Children</h2>
<p>This store is aimed at adults arranging household purchases. It is not directed at children under 18, and we do not knowingly collect their data. If you believe a child has shared data with us, contact vifaarkenya@gmail.com and we will delete it.</p>
<h2>9. Changes to this policy</h2>
<p>We review this policy at least annually and whenever our data practices change. The version number and effective date at the top always show which text applies; material changes are highlighted on the website for a reasonable period.</p>""",
    },
    {
        "slug": "cookie-policy",
        "title": "Cookie Policy",
        "meta_title": "Cookie Policy | Vifaar Appliances",
        "meta_description": (
            "Every cookie and tracker Vifaar Appliances uses, what each one "
            "does, how long it lasts, and how to accept, decline, or change "
            "your choice."
        ),
        "body": """<p><strong>Effective date: 13 October 2026 (version 1.1).</strong></p>
<p>Cookies and similar storage remember things between your visits. We use a small number of them, split into two groups: <strong>essential</strong> ones the shop cannot work without, and <strong>marketing</strong> ones that only ever load with your consent. Refusing marketing storage leaves the shop fully usable.</p>
<h2>1. Essential storage (always on)</h2>
<table>
<thead><tr><th>Name</th><th>Provider</th><th>Purpose</th><th>Duration</th></tr></thead>
<tbody>
<tr><td>estore_cart</td><td>Vifaar</td><td>Remembers your cart between visits so items are still there when you return.</td><td>90 days</td></tr>
<tr><td>csrftoken</td><td>Vifaar</td><td>Security token that proves form submissions come from our own pages.</td><td>Session (deleted when you close the browser)</td></tr>
<tr><td>vifaar-tracking-consent</td><td>Vifaar (your device only, never sent to us)</td><td>Remembers whether you accepted or declined tracking, which policy version you saw, and when you chose.</td><td>Until you clear it or change it via Cookie settings</td></tr>
<tr><td>vifaar-wishlist</td><td>Vifaar (your device only, never sent to us)</td><td>Remembers products you saved to your wishlist.</td><td>Until you clear it</td></tr>
</tbody>
</table>
<h2>2. Marketing storage (only with your consent)</h2>
<table>
<thead><tr><th>Name</th><th>Provider</th><th>Purpose</th><th>Duration</th></tr></thead>
<tbody>
<tr><td>_fbp</td><td>Meta</td><td>Distinguishes browsers for ad measurement.</td><td>About 90 days (set and controlled by Meta)</td></tr>
<tr><td>_fbc</td><td>Meta</td><td>Records visits arriving from Meta ads (Facebook/Instagram) for attribution.</td><td>About 2 years (set and controlled by Meta)</td></tr>
</tbody>
</table>
<p>When marketing storage is active, our Meta Pixel also sends event data (pages viewed, products viewed, searches, cart additions, wishlist saves, checkout steps, and completed hand-offs with order values) plus hashed contact details, IP address, and browser type to Meta, including a server-side copy for reliability. Full details are in our Privacy Policy. Meta's own handling is described in <a href="https://www.facebook.com/privacy/policy/" rel="noopener">Meta's Privacy Policy</a>.</p>
<h2>3. Your choice, and how to change it</h2>
<p>Grant or refuse marketing storage when we ask for your consent. Change your mind at any time by clearing this site's cookies and storage in your browser settings: that withdraws consent and stops further tracking immediately (data already sent to Meta stays subject to Meta's policy). The shop keeps working either way, but your cart may not persist without its cookie.</p>
<h2>4. Changes to this policy</h2>
<p>We re-check this table after every change to our tracking and update the version number and date above. The current version is 1.1, effective 13 October 2026.</p>""",
    },
    {
        "slug": "terms",
        "title": "Terms of Sale",
        "meta_title": "Terms of Sale | Vifaar Appliances",
        "meta_description": (
            "How ordering works at Vifaar Appliances: WhatsApp and email "
            "checkout, payment, delivery, returns, and warranties."
        ),
        "body": """<p><strong>Effective date: 13 October 2026 (version 1.1).</strong></p>
<p>These terms govern purchases from Vifaar Appliances ("Vifaar", "we", "us") through this website. By placing an order, you agree to them. If any term is unclear, ask us at vifaarkenya@gmail.com before ordering.</p>
<h2>1. How ordering works</h2>
<p>There is no online checkout or card payment on this website. You assemble your cart and send it to us through <strong>WhatsApp</strong> (opens a chat with your order summary) or <strong>email</strong> (we reply with an order reference). Your order is confirmed only when our team confirms availability, price, payment, and delivery with you directly. Prices are in Kenya Shillings (KES) and are confirmed at that point; website prices may change without notice until confirmation.</p>
<h2>2. Payment</h2>
<p>We accept M-Pesa, bank transfer, and cash on delivery on qualifying orders, as confirmed with you per order. Never share M-Pesa PINs or banking passwords with anyone claiming to be us; our team will never ask for them. Payment details are exchanged directly with you and are never entered on this website.</p>
<h2>3. Delivery and installation</h2>
<p>Delivery cost depends on your order and location and is confirmed before dispatch. Large appliances may require professional installation or two-person delivery, as marked on the product page; our team arranges this with you. Delivery times given are estimates. Risk passes to you on delivery; inspect items on arrival and report damage immediately.</p>
<h2>4. Returns and cooling off</h2>
<p>Unshipped orders can be cancelled free of charge - contact us as soon as possible. Delivered items are covered by our return policy within the stated cooling-off period, provided they are unused, in original packaging, and accompanied by proof of purchase. Customised, installed, or clearance items are final sale unless faulty. Nothing here limits your rights under Kenyan consumer protection law.</p>
<h2>5. Warranties</h2>
<p>Every product page states its warranty duration and terms, which apply in addition to your statutory rights. Warranty claims require proof of purchase; misuse, unauthorised repair, or commercial use beyond the product's rating voids cover unless stated otherwise.</p>
<h2>6. Acceptable use</h2>
<p>Use the site lawfully: no scraping that degrades the service, no false orders, no abuse of staff, and no misuse of reviews, questions, or uploads. Accounts or access used abusively may be restricted.</p>
<h2>7. Liability</h2>
<p>Where something goes wrong with your order, our liability is limited to the price you paid for the affected goods, and we are not liable for indirect or consequential losses such as lost profits or food spoiled by a delayed appliance. These limits do not apply to fraud or wilful misconduct, and nothing in these terms limits liability where Kenyan law forbids it, including your non-excludable consumer rights.</p>
<h2>8. Governing law and disputes</h2>
<p>These terms are governed by the laws of Kenya. Disputes are first addressed to vifaarkenya@gmail.com; unresolved disputes go to the competent courts in Nairobi, without limiting your right to seek redress from a consumer body.</p>
<h2>9. Changes</h2>
<p>We may update these terms; the version above always shows what applies. Orders already confirmed keep the terms in force at confirmation.</p>""",
    },
    {
        "slug": "return-policy",
        "title": "Return Policy",
        "meta_title": "Return Policy | Vifaar Appliances",
        "meta_description": (
            "How to cancel an unshipped order, return a delivered appliance "
            "within the 7-day cooling-off period, and how refunds work at "
            "Vifaar Appliances."
        ),
        "body": """<p><strong>Effective date: 14 October 2026 (version 1.2).</strong></p>
<p>Changed your mind, or found a fault with your appliance? This policy explains how cancellations, returns, and refunds work at Vifaar Appliances ("Vifaar", "we", "us"). To start any return, contact us at <strong>vifaarkenya@gmail.com</strong> or through the <a href="/pages/contact">contact page</a> with your order reference and, where relevant, photos of the item.</p>
<h2>1. Cancelling before dispatch</h2>
<p>Orders that have not shipped can be cancelled free of charge. Contact us as soon as possible - once an item is dispatched, cancellation is no longer possible and the cooling-off return below applies instead. Anything you had already paid is refunded in full when you cancel before dispatch.</p>
<h2>2. The 7-day cooling-off return</h2>
<p>Delivered items can be returned within <strong>7 days of delivery</strong>, provided they are unused, in their original packaging with all accessories and manuals, and accompanied by proof of purchase. To use the cooling-off return, contact us within the 7 days so we can record the request and arrange collection or drop-off at our shop on Taveta Road, Nairobi.</p>
<h2>3. Faulty or damaged items</h2>
<p>Inspect your appliance on delivery and report visible damage immediately so we can record it against the delivery. If a fault appears later, do not attempt your own repair - contact us with the model, a description of the fault, and photos where possible. Genuine faults are handled under this policy and the product's warranty terms: we arrange inspection and then repair, replacement, or refund depending on what the fault and the warranty allow.</p>
<h2>4. Items that cannot be returned</h2>
<p>Unless faulty, the following are final sale: items that have been installed or commissioned, items showing misuse or damage after delivery, and clearance items sold as final sale. Nothing here limits your rights under Kenyan consumer protection law, including your right to goods that match their description and work as expected.</p>
<h2>5. Refunds</h2>
<p>Refunds are arranged by our team directly with you - by M-Pesa, bank transfer, or store credit - and we confirm the amount, method, and timing with you before sending. Approved refunds cover what you paid for the goods; where delivery had already happened, we confirm with you whether any delivery cost applies. Nothing on this website moves money automatically: every refund is recorded by our staff with a written note.</p>
<h2>6. Exchanges</h2>
<p>If you would rather exchange than refund - for example a different size or model - tell us when you contact us and we will confirm availability and any price difference before arranging it.</p>""",
    },
    {
        "slug": "warranty",
        "title": "Warranty & Service",
        "meta_title": "Warranty & Service | Vifaar Appliances",
        "meta_description": (
            "Warranty cover on every Vifaar appliance, what is included and "
            "excluded, how to claim, and the installation and after-sales "
            "service behind it."
        ),
        "body": """<p><strong>Effective date: 14 October 2026 (version 1.2).</strong></p>
<p>Every appliance we sell carries warranty cover in addition to your statutory rights. The exact duration and terms for your model are stated on its product page - this page explains how that cover works in practice. For any warranty question, contact us at <strong>vifaarkenya@gmail.com</strong> or through the <a href="/pages/contact">contact page</a>.</p>
<h2>1. What the warranty covers</h2>
<p>Manufacturing defects that appear under normal household use during the stated warranty period: faulty components, defective workmanship, and failures that occur when the appliance is installed and operated as instructed. Selected refrigeration ranges carry an extended compressor warranty, which is stated on the product page where it applies.</p>
<h2>2. What the warranty does not cover</h2>
<p>Damage from misuse, neglect, or accident; faults caused by unauthorised repair or modification; use beyond the product's rating, including commercial use of a domestic-rated appliance, unless the product page states otherwise; and normal wear of consumable parts. Proof of purchase is required for every claim.</p>
<h2>3. How to make a claim</h2>
<p>Contact us with your proof of purchase, the product model, a description of the fault, and photos where possible. We assess the claim - which may mean an inspection at our shop or, for large installed appliances, an arranged visit - and then repair the appliance, replace it, or refund it, depending on what the fault and the warranty terms allow. Do not attempt your own repair first: unauthorised repair voids the cover.</p>
<h2>4. Installation and after-sales service</h2>
<p>Products that need professional installation or two-person delivery are marked on their product page, and our team arranges white-glove setup with you - see our <a href="/pages/delivery-installation">delivery and installation page</a> for detail. After installation, we remain your point of contact for operating guidance, care advice, and servicing for the life of the appliance.</p>""",
    },
    {
        "slug": "delivery-installation",
        "title": "Delivery & Installation",
        "meta_title": "Delivery & Installation | Vifaar Appliances",
        "meta_description": (
            "Where Vifaar delivers across Kenya, how delivery cost is "
            "confirmed, and how professional installation of large "
            "appliances is arranged."
        ),
        "body": """<p><strong>Effective date: 14 October 2026 (version 1.2).</strong></p>
<p>Vifaar Appliances ("Vifaar", "we", "us") delivers across Kenya from our shop on Taveta Road, Nairobi, and arranges professional installation for large appliances. There is no online checkout on this website: you send your cart through WhatsApp or email and our team confirms payment and delivery with you directly.</p>
<h2>1. Where we deliver</h2>
<p>We deliver within Nairobi and arrange courier delivery countrywide. At checkout you choose your county and delivery area so our team can quote and plan the right option for your order and location.</p>
<h2>2. How much delivery costs</h2>
<p>Delivery cost depends on your order and location, and is always confirmed with you before anything is dispatched - the website never charges delivery automatically. If the confirmed cost does not work for you, you can cancel the unshipped order free of charge.</p>
<h2>3. How long it takes</h2>
<p>Delivery times we give are estimates confirmed per order, not guarantees: stock availability, your location, and installation scheduling all play a part. Our team keeps you updated if anything changes after confirmation.</p>
<h2>4. Professional installation</h2>
<p>Products that require professional installation or two-person delivery are marked on their product page. For these, our team arranges white-glove setup with you - positioning and levelling, connection, and a basic functionality check where applicable - at a time you agree with us.</p>
<h2>5. Receiving your order</h2>
<p>Please have someone present to receive and inspect the appliance on arrival. Check for visible damage before the delivery team leaves and report anything immediately so it is recorded against the delivery. Risk passes to you on delivery.</p>
<h2>6. If delivery fails</h2>
<p>If nobody is available or the location cannot be reached, we contact you to rearrange. Repeated failed attempts may attract a re-delivery arrangement, which we agree with you before dispatching again.</p>""",
    },
    {
        "slug": "about",
        "title": "About Us",
        "meta_title": "About Us | Vifaar Appliances",
        "meta_description": (
            "Vifaar Appliances: quality home appliances in Nairobi with "
            "delivery across Kenya, professional installation, and "
            "after-sales service."
        ),
        "body": """<p><strong>Vifaar Appliances is a Nairobi home-appliance retailer serving households across Kenya.</strong> From our shop in the New Al Noor Exhibitions building along Taveta Road, we help customers choose appliances that fit their space, their budget, and everyday life - then stand behind them with delivery, installation, and after-sales service.</p>
<h2>What we sell</h2>
<p>Cooling and refrigeration, washers and dryers, cookers and ovens, smart TVs and entertainment, and everyday small appliances - sourced so that what you see on the product page is exactly what arrives at your door, with its warranty terms stated upfront.</p>
<h2>How shopping with us works</h2>
<p>Browse the <a href="/products">catalog</a> or a <a href="/bundles">bundle deal</a>, add items to your cart, and send the order through WhatsApp or email. Our team then confirms availability, price, payment (M-Pesa, bank transfer, or cash on delivery on qualifying orders), and delivery with you directly - there is no online payment step, and nothing ships before you have agreed the details.</p>
<h2>Why customers choose Vifaar</h2>
<ul>
<li>Genuine appliances with warranty cover stated on every product page.</li>
<li>Delivery across Kenya with the cost confirmed before dispatch.</li>
<li>Professional installation arranged for large appliances.</li>
<li>After-sales support from the same team that sold you the appliance.</li>
</ul>
<h2>Visit us</h2>
<p>Find us at the New Al Noor Exhibitions building along Taveta Road, Nairobi - see the <a href="/pages/contact">contact page</a> for directions, opening hours, phone, and email. We look forward to helping you make room for better living.</p>""",
    },
]
