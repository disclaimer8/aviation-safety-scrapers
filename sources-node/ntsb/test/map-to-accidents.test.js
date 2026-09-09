// The production mapper had no tests. test/parse.test.js exercises
// joinNtsbTables in src/parse.js, which src/cli.js does not import: it builds
// rows with mapToAccidents instead, and the two disagree — production drops
// events with an empty narr_accp and keeps the FIRST aircraft, joinNtsbTables
// keeps them and takes the last. A join bug in the real dump could not fail
// `npm test`.
const { mapToAccidents, toInt } = require('../src/cli');

const EV = {
  ev_id: 'ERA24LA101', ntsb_no: 'ERA24LA101', ev_date: '03/10/2024',
  ev_city: 'Bishoftu', ev_state: 'FL', ev_country: 'USA',
  inj_tot_f: '2', inj_tot_s: '1', inj_tot_m: '0',
};
const NARR = { ev_id: 'ERA24LA101', narr_accp: 'The pilot reported a loss of engine power.', narr_accf: 'Analysis.', narr_cause: 'Fuel starvation.' };

function tables(over = {}) {
  return {
    events: [EV], narratives: [NARR], aircraft: [], occurrences: [], ...over,
  };
}

describe('mapToAccidents (the mapper production actually calls)', () => {
  it('maps a complete event into the accidents schema', () => {
    const [row] = mapToAccidents(tables());
    expect(row.case_id).toBe('ERA24LA101');
    expect(row.event_date).toBe('2024-03-10');
    expect(row.state_country).toBe('FL, USA');
    expect(row.factual_narrative).toBe(NARR.narr_accp);
    expect(row.probable_cause).toBe('Fuel starvation.');
    expect(row.docket_url).toBe('https://carol.ntsb.gov/event/ERA24LA101');
  });

  it('drops events with no factual narrative', () => {
    expect(mapToAccidents(tables({ narratives: [{ ev_id: 'ERA24LA101', narr_accp: '   ' }] }))).toHaveLength(0);
    expect(mapToAccidents(tables({ narratives: [] }))).toHaveLength(0);
  });

  it('keeps the FIRST aircraft for an event, not the last', () => {
    // joinNtsbTables keeps the last; production keeps the first. Pinning the
    // production behaviour is the point of this file.
    const [row] = mapToAccidents(tables({
      aircraft: [
        { ev_id: 'ERA24LA101', acft_make: 'Cessna', acft_model: '172', regis_no: 'N1' },
        { ev_id: 'ERA24LA101', acft_make: 'Piper', acft_model: 'PA-28', regis_no: 'N2' },
      ],
    }));
    expect(row.aircraft).toBe('Cessna 172');
    expect(row.registration).toBe('N1');
  });

  it('normalizes a 2-digit-year NTSB date', () => {
    const [row] = mapToAccidents(tables({ events: [{ ...EV, ev_date: '08/19/85 14:30:00' }] }));
    expect(row.event_date).toBe('1985-08-19');
  });

  describe('injury counts', () => {
    it('carries real counts through, including a genuine zero', () => {
      const [row] = mapToAccidents(tables());
      expect(row.fatal).toBe(2);
      expect(row.serious).toBe(1);
      expect(row.minor).toBe(0);
    });

    // The bug: a blank injury count became 0, so "we do not know how many
    // died" was stored as "nobody died".
    it('leaves an unknown count null rather than calling it zero', () => {
      const [row] = mapToAccidents(tables({
        events: [{ ...EV, inj_tot_f: '', inj_tot_s: undefined, inj_tot_m: 'n/a' }],
      }));
      expect(row.fatal).toBeNull();
      expect(row.serious).toBeNull();
      expect(row.minor).toBeNull();
    });

    it('toInt tells a real zero from a missing value', () => {
      expect(toInt('0')).toBe(0);
      expect(toInt(0)).toBe(0);
      expect(toInt('')).toBeNull();
      expect(toInt(null)).toBeNull();
      expect(toInt(undefined)).toBeNull();
      expect(toInt('unknown')).toBeNull();
    });
  });
});

describe('assertNoZipSlip', () => {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const zlib = require('node:zlib');
  const { assertNoZipSlip } = require('../src/cli');

  // Builds a real (stored, uncompressed) ZIP with an arbitrary member name.
  // The `zip` CLI sanitises "../" on creation, so a hostile archive has to be
  // written byte by byte — which is exactly what an attacker would do.
  function zipWithMemberNames(names) {
    const body = Buffer.from('x');
    const crc = zlib.crc32 ? zlib.crc32(body) : 0x8c736521; // crc32 of "x"
    const locals = [];
    const centrals = [];
    let offset = 0;
    for (const name of names) {
      const nameBuf = Buffer.from(name, 'utf8');
      const local = Buffer.alloc(30 + nameBuf.length + body.length);
      local.writeUInt32LE(0x04034b50, 0);
      local.writeUInt16LE(20, 4);
      local.writeUInt32LE(crc >>> 0, 14);
      local.writeUInt32LE(body.length, 18);
      local.writeUInt32LE(body.length, 22);
      local.writeUInt16LE(nameBuf.length, 26);
      nameBuf.copy(local, 30);
      body.copy(local, 30 + nameBuf.length);
      locals.push(local);

      const central = Buffer.alloc(46 + nameBuf.length);
      central.writeUInt32LE(0x02014b50, 0);
      central.writeUInt16LE(20, 4);
      central.writeUInt16LE(20, 6);
      central.writeUInt32LE(crc >>> 0, 16);
      central.writeUInt32LE(body.length, 20);
      central.writeUInt32LE(body.length, 24);
      central.writeUInt16LE(nameBuf.length, 28);
      central.writeUInt32LE(offset, 42);
      nameBuf.copy(central, 46);
      centrals.push(central);
      offset += local.length;
    }
    const centralDir = Buffer.concat(centrals);
    const end = Buffer.alloc(22);
    end.writeUInt32LE(0x06054b50, 0);
    end.writeUInt16LE(names.length, 8);
    end.writeUInt16LE(names.length, 10);
    end.writeUInt32LE(centralDir.length, 12);
    end.writeUInt32LE(offset, 16);

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zipslip-'));
    const zip = path.join(dir, 'dump.zip');
    fs.writeFileSync(zip, Buffer.concat([...locals, centralDir, end]));
    return { dir, zip };
  }

  function check(names) {
    const { dir, zip } = zipWithMemberNames(names);
    try {
      assertNoZipSlip(zip);
      return null;
    } catch (e) {
      return e.message;
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  it('accepts an ordinary archive', () => {
    expect(check(['avall.mdb', 'docs/readme.txt'])).toBeNull();
  });

  it('refuses parent-directory traversal', () => {
    expect(check(['../../etc/cron.d/pwn'])).toMatch(/traversal/);
    expect(check(['a/../../b'])).toMatch(/traversal/);
  });

  it('refuses an absolute member path', () => {
    expect(check(['/etc/cron.d/pwn'])).toMatch(/absolute/);
  });

  it('refuses a Windows drive-letter path', () => {
    expect(check(['C:/Windows/pwn.exe'])).toMatch(/absolute/);
  });

  it('refuses an archive where only ONE member is hostile', () => {
    expect(check(['avall.mdb', '../../etc/cron.d/pwn'])).toMatch(/traversal/);
  });
});
