const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/csv.js'), 'utf8'), context);
const parse = text => JSON.parse(JSON.stringify(context.parseCSVRecipients(text)));
test('CSV supports BOM, reversed headers, CRLF and surrounding whitespace', () => {
  assert.deepEqual(parse('\uFEFFEmail,Name\r\na@example.com, Ada \r\n\r\n'), [{name:'Ada',email:'a@example.com'}]);
});
test('CSV supports commas, doubled quotes and embedded newlines', () => {
  assert.deepEqual(parse('name,email\n"Ada, ""A""",a@example.com\n"Multi\nline",m@example.com'), [{name:'Ada, "A"',email:'a@example.com'},{name:'Multi\nline',email:'m@example.com'}]);
});
test('CSV rejects malformed headers, columns, quoting and empty data', () => {
  for (const text of ['name,email','name,name\nA,B','name,email,extra\nA,B,C','name,email\nA','name,email\n"A,B','name,email\nA"B,C','name,email\n"A"x,B']) assert.throws(()=>parse(text));
});
test('CSV preserves individually invalid fields for backend validation', () => {
  assert.deepEqual(parse('name,email\nAda,bad\n,invalid\n,'), [{name:'Ada',email:'bad'},{name:'',email:'invalid'},{name:'',email:''}]);
});
test('CSV enforces recipient cap', () => {
  assert.equal(parse('name,email\n' + 'Ada,a@example.com\n'.repeat(10000)).length,10000);
  assert.throws(()=>parse('name,email\n'+'Ada,a@example.com\n'.repeat(10001)));
});
