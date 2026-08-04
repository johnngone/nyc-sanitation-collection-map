# Phase 3: DSNY lookup investigation

The official collection schedule page is:

https://www.nyc.gov/assets/dsny/site/collection-schedule-lookup

The page is client-rendered. Its indexed HTML exposes the page but not the
request made after an address is submitted. No endpoint, request body, header
set, cookie requirement, or identifier type is being guessed here.

The repository includes `scripts/test_dsny_lookup.py`. It requires the exact
public JSON endpoint and parameter shape observed from the official page.
It uses a descriptive user agent, a timeout, one request, and prints the raw
JSON response. It does not bypass access controls or retry.

## Required evidence before using the script

In a normal desktop browser:

1. Open the official lookup page, open Developer Tools → Network, and enable the Fetch/XHR filter.
2. Enter one test address and submit it. Record the request URL, method, request body/query parameters, response content type, and response JSON. Do not submit more than one test address during this inspection.

Do not copy cookies, authorization tokens, CAPTCHA values, or other secrets into the repository. If the request is not public or is protected by a rate limit/access control, stop and report that result.

