<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="html" encoding="UTF-8" indent="yes"/>
  <xsl:template match="/">
    <html lang="en">
      <head>
        <meta charset="UTF-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <meta name="color-scheme" content="light"/>
        <title>Buying guides &amp; tips | Blogs</title>
        <style>
          :root {
            --color-background: #FAFAFA;
            --color-foreground: #09090B;
            --color-card: #FFFFFF;
            --color-muted: #E8ECF0;
            --color-muted-foreground: #475569;
            --color-border: #E4E4E7;
            --color-accent: #EC4899;
            --color-on-accent: #000000;
            --space-1: 8px;
            --space-2: 16px;
            --space-3: 24px;
            --space-4: 32px;
            --space-5: 48px;
            --radius: 12px;
          }
          * { box-sizing: border-box; }
          body {
            margin: 0;
            background: var(--color-background);
            color: var(--color-foreground);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            font-size: 16px;
            line-height: 1.6;
          }
          .skip-link {
            position: absolute;
            left: var(--space-2);
            top: -48px;
            background: var(--color-foreground);
            color: var(--color-background);
            padding: var(--space-1) var(--space-2);
            border-radius: 8px;
            transition: top 150ms ease;
          }
          .skip-link:focus { top: var(--space-1); }
          :focus-visible {
            outline: 3px solid var(--color-foreground);
            outline-offset: 2px;
          }
          header.hero {
            border-bottom: 1px solid var(--color-border);
            background: var(--color-card);
          }
          .wrap {
            max-width: 1120px;
            margin: 0 auto;
            padding: var(--space-4) var(--space-2);
          }
          .eyebrow {
            display: inline-block;
            background: var(--color-accent);
            color: var(--color-on-accent);
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            padding: 4px 12px;
            border-radius: 999px;
          }
          h1 {
            font-family: Georgia, "Times New Roman", serif;
            font-size: 40px;
            line-height: 1.15;
            letter-spacing: -0.01em;
            margin: var(--space-2) 0 var(--space-1) 0;
            text-wrap: balance;
          }
          .lede {
            color: var(--color-muted-foreground);
            font-size: 18px;
            max-width: 62ch;
            margin: 0 0 var(--space-2) 0;
          }
          .count {
            color: var(--color-muted-foreground);
            font-size: 14px;
            margin: 0;
          }
          main .grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: var(--space-2);
            padding-top: var(--space-3);
            padding-bottom: var(--space-5);
          }
          article.card {
            background: var(--color-card);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: var(--space-3) var(--space-2);
            display: flex;
            flex-direction: column;
            gap: var(--space-1);
          }
          article.card h2 {
            font-family: Georgia, "Times New Roman", serif;
            font-size: 22px;
            line-height: 1.3;
            margin: 0;
            text-wrap: balance;
          }
          article.card h2 a {
            color: inherit;
            text-decoration: underline;
            text-decoration-thickness: 1px;
            text-underline-offset: 3px;
            display: inline-block;
            padding: 6px 0;
            min-height: 44px;
          }
          article.card h2 a:hover { text-decoration-thickness: 2px; }
          .meta {
            display: flex;
            flex-wrap: wrap;
            gap: var(--space-1);
            align-items: center;
            font-size: 14px;
            color: var(--color-muted-foreground);
          }
          .badge {
            background: var(--color-muted);
            color: var(--color-muted-foreground);
            border-radius: 999px;
            padding: 2px 10px;
            font-size: 13px;
            font-weight: 600;
          }
          .excerpt { margin: 0; }
          .read-more {
            display: inline-block;
            min-height: 44px;
            line-height: 44px;
            font-weight: 600;
            color: inherit;
          }
          .empty {
            background: var(--color-card);
            border: 1px dashed var(--color-border);
            border-radius: var(--radius);
            padding: var(--space-4) var(--space-2);
            text-align: center;
            color: var(--color-muted-foreground);
          }
          footer.site {
            border-top: 1px solid var(--color-border);
            background: var(--color-card);
            color: var(--color-muted-foreground);
            font-size: 14px;
          }
          footer.site a { color: inherit; }
          @media (min-width: 768px) {
            .wrap { padding-left: var(--space-3); padding-right: var(--space-3); }
            h1 { font-size: 56px; }
            main .grid { grid-template-columns: 1fr 1fr; gap: var(--space-3); }
          }
          @media (min-width: 1024px) {
            main .grid { grid-template-columns: 1fr 1fr 1fr; }
          }
          @media (prefers-reduced-motion: reduce) {
            * { transition: none; }
          }
        </style>
      </head>
      <body>
        <a class="skip-link" href="#articles">Skip to articles</a>
        <header class="hero">
          <div class="wrap">
            <span class="eyebrow">Blogs</span>
            <h1>Buying guides &amp; tips</h1>
            <p class="lede"><xsl:value-of select="/rss/channel/description"/></p>
            <p class="count"><xsl:value-of select="count(/rss/channel/item)"/> posts</p>
          </div>
        </header>
        <main>
          <div class="wrap">
            <div class="grid" id="articles">
              <xsl:for-each select="/rss/channel/item">
                <article class="card">
                  <div class="meta">
                    <span class="badge">Guide</span>
                    <time><xsl:value-of select="pubDate"/></time>
                  </div>
                  <h2>
                    <a href="{link}"><xsl:value-of select="title"/></a>
                  </h2>
                  <p class="excerpt"><xsl:value-of select="description"/></p>
                  <a class="read-more" href="{link}">Read article</a>
                </article>
              </xsl:for-each>
              <xsl:if test="count(/rss/channel/item) = 0">
                <div class="empty">
                  <p>No posts have been published yet. Please check back soon.</p>
                </div>
              </xsl:if>
            </div>
          </div>
        </main>
        <footer class="site">
          <div class="wrap">
            <p>Blogs is generated from the store feed. Browse every article at <a href="{/rss/channel/link}"><xsl:value-of select="/rss/channel/link"/></a>.</p>
          </div>
        </footer>
      </body>
    </html>
  </xsl:template>
</xsl:stylesheet>
