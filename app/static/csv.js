'use strict';
// Strict CSV parser: quoted commas, doubled quotes, embedded newlines, CRLF and BOM.
function parseCSVRecipients(text) {
  text = text.replace(/^\uFEFF/, '');
  const rows = []; let row = [], field = '', quoted = false, closed = false;
  function pushRow() {
    row.push(field);
    if (row.length > 1 || row[0].trim() !== '') rows.push(row);
    row = []; field = ''; closed = false;
  }
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') {field += '"'; i++;}
        else {quoted = false; closed = true;}
      } else field += char;
    } else if (char === ',' || char === '\n' || char === '\r') {
      if (char === ',') {row.push(field); field = ''; closed = false;}
      else {if (char === '\r' && text[i + 1] === '\n') i++; pushRow();}
    } else if (char === '"') {
      if (field !== '' || closed) throw new Error('Invalid CSV quoting. Quotes must enclose the complete field.');
      quoted = true;
    } else {
      if (closed) throw new Error('Unexpected text after a quoted CSV field.');
      field += char;
    }
  }
  if (quoted) throw new Error('CSV contains an unclosed quoted field.');
  if (row.length || field !== '' || closed) pushRow();
  const headers = (rows.shift() || []).map(value => value.trim().toLowerCase());
  if (headers.length !== 2 || !headers.includes('name') || !headers.includes('email')) {
    throw new Error('CSV must have exactly two columns: name and email.');
  }
  if (!rows.length || rows.length > 10000) throw new Error('CSV must contain 1–10,000 recipients.');
  return rows.map((values, index) => {
    if (values.length !== 2) throw new Error(`CSV recipient row ${index + 1} must have two columns.`);
    return {name: values[headers.indexOf('name')].trim(), email: values[headers.indexOf('email')].trim()};
  });
}
