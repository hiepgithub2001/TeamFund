import assert from 'node:assert/strict';
import test from 'node:test';
import { amountAndTitle, money, parseAmount } from '../src/format.js';
import { dicts, t } from '../src/i18n.js';

test('parseAmount understands common VND formats', () => {
  assert.equal(parseAmount('200000'), 200000);
  assert.equal(parseAmount('200.000'), 200000);
  assert.equal(parseAmount('200,000đ'), 200000);
  assert.equal(parseAmount('200k'), 200000);
  assert.equal(parseAmount('1.5tr'), 1500000);
  assert.equal(parseAmount('2m'), 2000000);
  assert.equal(parseAmount('abc'), null);
  assert.equal(parseAmount('0'), null);
  assert.deepEqual(amountAndTitle('500k Tiền sân'), [500000, 'Tiền sân']);
});

test('money formats per language', () => {
  assert.equal(money(200000, 'vi'), '200.000đ');
  assert.equal(money(200000, 'en'), '200,000đ');
});

test('English and Vietnamese have the same keys and placeholders', () => {
  const en = Object.keys(dicts.en).sort();
  const vi = Object.keys(dicts.vi).sort();
  assert.deepEqual(vi, en);
  const ph = (s) => (s.match(/\{\w+\}/g) || []).sort().join();
  for (const k of en) assert.equal(ph(dicts.vi[k]), ph(dicts.en[k]), k);
  assert.equal(t('vi', 'setdue.done', { day: 5 }), '✅ Đã đặt hạn đóng là ngày 5.');
});
