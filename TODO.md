# KATNIP TODO

## High Priority

- Keystone repository template setup ([cookiecutter](https://github.com/Keystone-Strategy/ks-cookiecutter-python))
- Cookie banner detection
    -   Investigate why consent banners do not consistently appear in testing browser (Appears to be related to VPN usage? User-Agent?)
    -   Improve browser realism (fresh profiles, fingerprint consistency, etc.).
- Cloudflare security verification handling
    -   Detect Cloudflare challenge pages (e.g. Fandom).
    -   Gracefully interrupt journeys while preserving artifacts.
    -   Expand support for additional anti-bot obstacles.
- Click journey
    -   Scroll through pages while opening eligible links in NEW tabs through buttons and hyperlinks.

## Reporting

- Build out the technology identification database
    -   Map hosts and URL patterns to human-readable technologies.
    -   Include major analytics, advertising, consent, CDP, and session replay vendors commonly encountered.
- Add cookie details to CSV and JSON exports
    -   First-party vs third-party cookies.
- User-action report tied to network requests
    -   Log timestamps in human-time and browser recording time for when clicks or scolling actions occur, correlate to network requests based on timestamps (post-process .mitm dump)
- Raw data export to Word Doc memo format

## Documentation

- Create detailed onboarding documentation
    -   Dependency installation
    -   WireGuard setup through ProtonVPN configuration
    -   Chrome profile configuration (copy from existing)
    -   mitmproxy certificate installation (copy from existing)
    -   Native recorder build
    -   Creating custom journeys
    -   Extending the technology catalog

## Future Ideas

-  Visualizaiton of request timing versus user actions
-  Additional consent providers (TrustArc, Cookiebot, etc.)
-  More journey types (e.g., search, forms)
-  Screenshot capture at specific stages of recording around consent banner interactions
-  Timeline visualization of user actions + event requests (should see spikes at page load and clicks etc.)
