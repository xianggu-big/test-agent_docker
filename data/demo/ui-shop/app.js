// ui-shop demo：待办清单（黑包阶段3 UI 黑盒验收载体）
// 埋缺陷：removeTodo 未定义——「删除」按钮点击后抛 ReferenceError，项永不消失（按钮失效）。
function makeItem(text) {
  const li = document.createElement('li');
  const span = document.createElement('span');
  span.textContent = text;
  const del = document.createElement('button');
  del.className = 'del';
  del.setAttribute('aria-label', '删除 ' + text);
  del.textContent = '删除';
  // [缺陷] removeTodo 在本文件中从未定义 → 点击删除无任何可见效果
  del.addEventListener('click', function () { removeTodo(li); });
  li.appendChild(span);
  li.appendChild(del);
  return li;
}

// 预置一项（快照与场景的确定性锚点；命名刻意避开 LLM 高频自选词，防重名 strict violation）
document.getElementById('todo-list').appendChild(makeItem('预置任务-alpha'));

document.getElementById('add-btn').addEventListener('click', function () {
  const input = document.getElementById('new-todo');
  const v = (input.value || '').trim();
  if (!v) return;
  document.getElementById('todo-list').appendChild(makeItem(v));
  input.value = '';
});
