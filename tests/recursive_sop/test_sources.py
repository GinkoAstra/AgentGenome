"""Source intake evidence: imports text, never follows execution instructions."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sop.sources import collect_sources


class SourceCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sop-sources-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def collect(self, text):
        return collect_sources(self.write('SKILL.md', text))

    def codes(self, result):
        return {item['code'] for item in result['diagnostics']}

    def test_recursive_relative_sources_have_stable_ids_hashes_and_lines(self):
        self.write('docs/interface.md', 'Interface\n[fixed](../fixed.py)\n[return](../SKILL.md)\n')
        script = self.write('fixed.py', 'raise RuntimeError("must never execute")\n')
        result = self.collect('Skill\n[interface](docs/interface.md)\n[script](fixed.py)\n')
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual(result['root_id'], 'SKILL.md')
        self.assertEqual([file['id'] for file in result['files']], ['SKILL.md', 'docs/interface.md', 'fixed.py'])
        file = result['files'][-1]
        self.assertEqual(file['relative_path'], 'fixed.py')
        self.assertEqual(file['path'], str(script))
        self.assertEqual(file['sha256'], hashlib.sha256(script.read_bytes()).hexdigest())
        self.assertEqual(file['lines'], [{'line': 1, 'text': 'raise RuntimeError("must never execute")'}])
        self.assertEqual(result, collect_sources(self.root / 'SKILL.md'))

    def test_code_fences_inline_code_comments_and_script_content_are_not_links(self):
        self.write('script.sh', 'printf "[not a reference](missing-inside-code.md)"\n')
        result = self.collect('''# Skill
```sh
echo "[not a source](absent.md)"
```
~~~~
[not either](also-absent.md)
~~~
~~~~
`[inline](not-a-source.md)`
`` [two ticks](not-two.md) ` ``
    [indented code](not-indented.md)
<!-- [comment](not-comment.md) -->
\\[escaped](not-escaped.md)
[script](script.sh)
''')
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual([file['id'] for file in result['files']], ['SKILL.md', 'script.sh'])

    def test_standard_titles_reference_links_angles_and_encoded_names(self):
        self.write('docs/a file.md', 'Some documentation\n')
        self.write('docs/schema(1).json', '{"columns": []}\n')
        self.write('docs/form.yaml', 'fields: []\n')
        result = self.collect('''[one](<docs/a file.md> "read (carefully)")
[two](docs/schema(1).json 'schema')
[three][ FORM ]
[FORM]
[form]: docs/form.yaml "form interface"
[encoded](docs/a%20file.md)
''')
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual({file['id'] for file in result['files']}, {'SKILL.md', 'docs/a file.md', 'docs/schema(1).json', 'docs/form.yaml'})

    def test_nested_labels_and_list_continuations_still_import_sources(self):
        self.write('schema.json', '{}\n')
        result = self.collect('- Attachment:\n    [schema [v1]](schema.json)\n')
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual([file['id'] for file in result['files']], ['SKILL.md', 'schema.json'])

    def test_encoded_query_and_fragment_are_not_literal_source_filenames(self):
        self.write('schema?rev=1.md', 'must not import\n')
        self.write('schema#section.md', 'must not import\n')
        result = self.collect('[query](schema%3Frev=1.md)\n[fragment](schema%23section.md)\n')
        self.assertEqual(len(result['diagnostics']), 2)
        self.assertEqual(self.codes(result), {'unsupported_source_reference'})
        self.assertEqual(len(result['files']), 1)

    def test_missing_references_and_unsupported_binary_remain_located_diagnostics(self):
        self.write('exists.png', 'not a text interface')
        result = self.collect('# Skill\n[missing](missing.md)\n![binary](exists.png)\n[bad ref][not-defined]\n')
        self.assertEqual(self.codes(result), {'missing_source', 'unsupported_source', 'invalid_source_reference'})
        self.assertEqual({item['line'] for item in result['diagnostics']}, {2, 3, 4})
        self.assertTrue(all(item['source_id'] == 'SKILL.md' for item in result['diagnostics']))
        self.assertEqual(len(result['files']), 1)

    def test_invalid_utf8_and_binary_controls_are_not_source_text(self):
        (self.root / 'bad.txt').write_bytes(b'\xff')
        (self.root / 'binary.py').write_bytes(b'print(1)\x00')
        result = self.collect('[encoding](bad.txt)\n[binary](binary.py)\n')
        self.assertEqual(len(result['diagnostics']), 2)
        self.assertEqual(self.codes(result), {'invalid_source_encoding'})
        self.assertEqual(len(result['files']), 1)

    def test_absolute_remote_query_fragment_and_outside_references_fail_closed(self):
        result = self.collect('''[absolute](/etc/passwd)
[remote](https://example.com/material.md)
[file URI](file:///etc/passwd)
[query](interface.md?revision=1)
[fragment](interface.md#contract)
[escape](../outside.md)
[encoded escape](%2e%2e/outside.md)
[Windows](C:\\Windows\\source.txt)
[anchor](#purpose)
[self]()
''')
        self.assertEqual(len(result['diagnostics']), 8)
        self.assertEqual(self.codes(result), {'unsupported_source_reference', 'unsafe_source'})
        self.assertEqual(len(result['files']), 1)

    def test_symlink_hardlink_and_directory_symlink_are_rejected(self):
        original = self.write('original.txt', 'content\n')
        (self.root / 'linked.txt').symlink_to(original)
        os.link(original, self.root / 'hard.txt')
        self.write('real/child.md', 'material\n')
        (self.root / 'alias').symlink_to(self.root / 'real', target_is_directory=True)
        result = self.collect('[symbolic](linked.txt)\n[hard](hard.txt)\n[parent](alias/child.md)\n')
        self.assertEqual(len(result['diagnostics']), 3)
        self.assertEqual(self.codes(result), {'unsafe_source'})
        self.assertEqual(len(result['files']), 1)

    def test_invalid_primary_source_is_an_explicit_diagnostic(self):
        result = collect_sources(self.root / 'missing.md')
        self.assertEqual(result['root_id'], 'missing.md')
        self.assertEqual(result['files'], [])
        self.assertEqual(self.codes(result), {'missing_source'})
        self.write('folder.md/file.txt', 'child\n')
        result = collect_sources(self.root / 'folder.md')
        self.assertEqual(result['files'], [])
        self.assertTrue(result['diagnostics'])

    def test_import_is_read_only_and_source_changes_change_identity(self):
        reference = self.write('script.sh', 'touch must-not-exist\n')
        source = self.write('SKILL.md', '[run](script.sh)\n')
        before = {file: file.read_bytes() for file in (reference, source)}
        first = collect_sources(source)
        self.assertEqual({file: file.read_bytes() for file in before}, before)
        self.assertFalse((self.root / 'must-not-exist').exists())
        reference.write_text('exit 7\n', encoding='utf-8')
        second = collect_sources(source)
        self.assertEqual(first['files'][0], second['files'][0])
        self.assertNotEqual(first['files'][1]['sha256'], second['files'][1]['sha256'])
        self.assertEqual(first['files'][1]['id'], second['files'][1]['id'])

    def test_file_count_limit_has_a_visible_gap(self):
        for name in ('a.txt', 'b.txt', 'c.txt'):
            self.write(name, name)
        with patch('sop.sources.MAX_FILES', 3):
            result = self.collect('[a](a.txt)\n[b](b.txt)\n[c](c.txt)\n')
        self.assertEqual(len(result['files']), 3)
        self.assertEqual(self.codes(result), {'source_limit'})
        self.assertEqual(result['diagnostics'][0]['reference'], 'c.txt')

    def test_file_and_total_byte_limits_are_enforced(self):
        self.write('large.txt', 'x' * 64)
        with patch('sop.sources.MAX_FILE_BYTES', 32):
            result = self.collect('[large](large.txt)\n')
        self.assertEqual(self.codes(result), {'source_limit'})
        self.assertEqual(len(result['files']), 1)
        self.write('a.txt', 'x' * 25)
        self.write('b.txt', 'y' * 25)
        with patch('sop.sources.MAX_TOTAL_BYTES', 65):
            result = self.collect('[a](a.txt)\n[b](b.txt)\n')
        self.assertEqual(self.codes(result), {'source_limit'})
        self.assertEqual([file['id'] for file in result['files']], ['SKILL.md', 'a.txt'])

    def test_malformed_explicit_link_is_not_silently_ignored(self):
        result = self.collect('[broken](not-closed.md\n[reference][missing]\n')
        self.assertEqual(self.codes(result), {'invalid_source_reference'})
        self.assertEqual(len(result['diagnostics']), 2)


if __name__ == '__main__':
    unittest.main()
