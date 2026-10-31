// Keeps local previews (`mint dev`) out of the site's PostHog analytics. Mintlify's local preview
// loads the PostHog integration from docs.json just as the deployed site does, so on a loopback
// host this sets PostHog's own opt-out flag, which it reads before every capture. The key ends in
// the `apiKey` from docs.json and must change with it.
if (["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)) {
  window.localStorage.setItem(
    "__ph_opt_in_out_phc_kS25RNRJszpGcehmv4pyX5WiPDDR892G3eGM2CTM7pib",
    "0",
  );
}
