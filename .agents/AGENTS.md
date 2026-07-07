# Agent Rules — Workspace Customizations

These rules govern agent behavior for this repository. All AI coding assistants must read and adhere to these guidelines.

## Cost & Credit Management

- **Credit Preservation**: Avoid running automated E2E/manual tests that consume third-party scraper credits (such as Firecrawl, ScrapeGraphAI, or proxy credits) without explicit user permission.
- **Local Testing**:
  - When verifying logic or routing, bypass primary credits by unsetting keys in the execution context (e.g. `FIRECRAWL_API_KEY=""`).
  - Use `PRICE_OVERRIDE` or simulation mocks where possible to verify pipeline steps without calling external scrapers.
- **State Preservation**: Do not delete or reset `deals_state.json` or config states during testing, as it preserves historical alert states.

## Scraper Requirements

- **Shipping Priority**: Always parse or prompt the extraction of the **Shipping** option's stock availability status rather than the local pickup status.
- **Variant Selection**: Always target variant/storage selections explicitly (e.g. 512GB Deep Blue).
