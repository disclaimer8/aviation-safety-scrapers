'use strict';
// report_pdf_final is an absolute URL resolved from an href on MAK's own
// pages, so this fetcher follows a target the site chooses. On the ingest box
// that reaches the LAN and the cloud metadata endpoint.
const { assertPublicTarget, isPrivateAddress } = require('../src/scrape');

describe('isPrivateAddress', () => {
  it.each([
    '127.0.0.1', '10.0.0.5', '192.168.1.10', '172.16.0.1', '172.31.255.255',
    '169.254.169.254', '0.0.0.0', '100.64.0.1', '224.0.0.1',
    '::1', '::', 'fe80::1', 'fd00::1', 'fc00::1',
  ])('refuses %s', (ip) => expect(isPrivateAddress(ip)).toBe(true));

  it.each(['1.1.1.1', '8.8.8.8', '93.184.216.34', '172.32.0.1', '2606:4700::1111'])(
    'allows %s', (ip) => expect(isPrivateAddress(ip)).toBe(false));
});

describe('assertPublicTarget', () => {
  it.each([
    'http://127.0.0.1:8080/secret.pdf',
    'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.5/report.pdf',
    'http://[::1]/report.pdf',
  ])('rejects %s', async (url) => {
    await expect(assertPublicTarget(url)).rejects.toThrow(/private\/internal/);
  });

  it.each(['file:///etc/passwd', 'ftp://host/x', 'javascript:alert(1)'])(
    'rejects the %s scheme', async (url) => {
      await expect(assertPublicTarget(url)).rejects.toThrow(/refusing to fetch/);
    });

  it('rejects an unparseable URL', async () => {
    await expect(assertPublicTarget('not a url')).rejects.toThrow(/unparseable/);
  });

  it('allows a public literal address', async () => {
    await expect(assertPublicTarget('https://1.1.1.1/report.pdf')).resolves.toBeUndefined();
  });

  it('leaves a name that will not resolve to fetch itself', async () => {
    // The guard must not invent a DNS error or swallow the real one.
    await expect(assertPublicTarget('https://nx.invalid.test/x')).resolves.toBeUndefined();
  });
});
