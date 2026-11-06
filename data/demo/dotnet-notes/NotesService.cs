using System;
using System.Collections.Generic;

namespace NotesDemo
{
    /// <summary>
    /// 备注服务（白包阶段5 .NET Tier3 兜底验收 demo）。
    /// 埋问题：Save() 里空 catch 吞异常 + Add 超长函数。
    /// </summary>
    public class NotesService
    {
        private readonly List<string> _notes = new List<string>();

        public int Add(string title, string body, string tag, DateTime createdAt,
                       string author, int priority, bool pinned, string color)
        {
            if (title == null) { throw new ArgumentNullException(nameof(title)); }
            if (body == null) { body = ""; }
            if (tag == null) { tag = "general"; }
            if (author == null) { author = "anonymous"; }
            if (priority < 0) { priority = 0; }
            if (priority > 9) { priority = 9; }
            var note = string.Format("{0}|{1}|{2}|{3}|{4}|{5}|{6}|{7}",
                title, body, tag, createdAt, author, priority, pinned, color);
            _notes.Add(note);
            try
            {
                Console.WriteLine("saved note {0}", _notes.Count);
            }
            catch (Exception ex)
            {
            }
            return _notes.Count;
        }

        public string Get(int index)
        {
            if (index < 0 || index >= _notes.Count) { return null; }
            return _notes[index];
        }
    }
}
