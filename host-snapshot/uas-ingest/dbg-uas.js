const X = require('xlsx');
for (const f of ['fy2020_q2_uas_sightings.xlsx','fy2021_q3_uas_sightings.xlsx','fy2022_q1_uas_sightings.xlsx']) {
  const wb = X.readFile(f, { cellDates: true });
  const aoa = X.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { header: 1, blankrows: false });
  console.log(`\n${f}: header=${JSON.stringify(aoa[0])}`);
  console.log(`  date vals rows 1-3: ${JSON.stringify([aoa[1]&&aoa[1][0], aoa[2]&&aoa[2][0], aoa[3]&&aoa[3][0]])}`);
  console.log(`  typeof: ${aoa[1] && typeof aoa[1][0]}`);
}
