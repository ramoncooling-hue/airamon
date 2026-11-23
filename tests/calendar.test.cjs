const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const calendar = script.slice(script.indexOf('function renderCalendar(){'), script.indexOf('function renderOrchestra(){'));
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));

function render(events = [], options = {}) {
  const nodes = {};
  const el = id => nodes[id] ||= {innerHTML:'', textContent:'', onclick:null, showModal(){ this.open = true; }, close(){ this.open = false; }};
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [2026, 8, 28, 12])); }
  }
  const S = {evFilter:'all', calendarMonth:null, _events:events, ...options};
  const context = vm.createContext({S, el, esc:escape, Date:Clock, document:{querySelectorAll:()=>[]}});
  vm.runInContext(calendar, context);
  context.renderCalendar();
  return {S, nodes, context, markup:()=>nodes.calBox.innerHTML};
}

test('all inline application JavaScript parses', () => { new vm.Script(script); });

test('September uses five complete Monday-first weeks with dated selectable cells', () => {
  const app = render();
  assert.equal((app.markup().match(/class="calendar-day/g) || []).length, 35);
  assert.match(app.markup(), /data-date="2026-08-31"/);
  assert.match(app.markup(), /data-date="2026-10-04"/);
  assert.match(app.markup(), /aria-label="2026-09-28/);
});

test('reminders cover today through day 13 inclusive and sort dates and times', () => {
  const events = [
    {date:'2026-10-12', category:'hw', title:'Outside'},
    {date:'2026-10-11', category:'exam', title:'Last day'},
    {date:'2026-09-27', category:'class', title:'Past'},
    {date:'2026-09-28', category:'class', title:'Later', time_range:'18:00'},
    {date:'2026-09-28', category:'class', title:'Earlier', time_range:'09:00'}
  ];
  const app = render(events);
  const reminders = app.markup().split('calendar-reminders')[1];
  assert.match(reminders, /2026-09-28 至 2026-10-11/);
  assert.match(reminders, /Last day/);
  assert.doesNotMatch(reminders, /Outside|Past/);
  assert.ok(reminders.indexOf('Earlier') < reminders.indexOf('Later'));
});

test('category filters affect calendar and reminders but retain an empty month grid', () => {
  const events = [{date:'2026-09-28', category:'class', title:'Class'}, {date:'2026-09-30', category:'hw', title:'Homework'}];
  const app = render(events, {evFilter:'hw'});
  assert.match(app.markup(), /Homework/);
  assert.doesNotMatch(app.markup(), />Class</);
  const empty = render(events, {evFilter:'personal'});
  assert.equal((empty.markup().match(/class="calendar-day/g) || []).length, 35);
  assert.match(empty.markup(), /未来 14 天暂无已记录事项/);
});

test('six-week months, leap days and year navigation stay correct', () => {
  const app = render([], {calendarMonth:'2026-11'});
  assert.equal((app.markup().match(/class="calendar-day/g) || []).length, 42);
  const leap = render([], {calendarMonth:'2028-02'});
  assert.match(leap.markup(), /data-date="2028-02-29"/);
  const year = render([], {calendarMonth:'2026-12'});
  year.nodes.calNext.onclick();
  assert.equal(year.S.calendarMonth, '2027-01');
  year.nodes.calPrev.onclick();
  assert.equal(year.S.calendarMonth, '2026-12');
  year.nodes.calToday.onclick();
  assert.equal(year.S.calendarMonth, '2026-09');
});

test('source text is escaped and invalid dates do not roll over into reminders', () => {
  const app = render([{date:'2026-09-31', category:'hw', title:'Invalid'}, {date:'2026-09-28', category:'personal', title:'<script>Bad</script>', note:'<img>'}]);
  assert.match(app.markup(), /&lt;script&gt;Bad&lt;\/script&gt;/);
  assert.doesNotMatch(app.markup(), /<script>|Invalid/);
});
