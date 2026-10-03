"""Check exact PDF tool versions and exercise the monthly source-reader commands."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

VERSION = '26.07.0'
TEXT = (
    'Parser installation fixture: page one contains a complete readable paragraph. '
    'These synthetic values only test extraction: estimate 0.73; confidence interval 0.60 to 0.89; participants 1234.',
    'Parser installation fixture: page two verifies multi-page text and page boundaries. '
    'No study evidence is represented here. The expected plain text and numerical tokens must survive PDF extraction.',
)


def sample_pdf():
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for i, paragraph in enumerate(TEXT):
        # Wrap at words so substantive text remains inside the page boundary.
        import textwrap
        lines = textwrap.wrap(paragraph, width=75)
        stream = ('BT /F1 11 Tf 40 750 Td 16 TL\n' + '\n'.join(
            f'({line}) Tj T*' for line in lines) + '\nET\n').encode('ascii')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5+i*2} 0 R >>'.encode())
        objects.append(b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'endstream')
    pdf = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf += f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    start = len(pdf)
    pdf += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    for offset in offsets[1:]:
        pdf += f'{offset:010d} 00000 n \n'.encode()
    pdf += f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
    return bytes(pdf)


def run(command, *, accepted_returncodes=(0,)):
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if result.returncode not in accepted_returncodes:
        raise RuntimeError(f'PDF tool failed: {Path(command[0]).name}: {result.stderr}')
    return result.stdout, result.stderr


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify(bin_dir):
    folder = Path(bin_dir).resolve()
    tools = {name: folder/name for name in ('pdfinfo', 'pdftotext', 'pdfdetach')}
    receipt = {'required_version': VERSION, 'tools': {}}
    for name, executable in tools.items():
        # pdfdetach uses its documented usage exit (99) for -v. Actual PDF
        # operations below must still exit successfully.
        stdout, stderr = run([str(executable), '-v'], accepted_returncodes=(0, 99))
        match = re.search(rf'(?m)^{name} version ([0-9.]+)$', stdout+'\n'+stderr)
        if not match or match.group(1) != VERSION:
            raise RuntimeError(f'{name} must be exactly {VERSION}')
        receipt['tools'][name] = {'version': match.group(1), 'sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}
    pdf = sample_pdf()
    with tempfile.TemporaryDirectory(prefix='monthly-poppler-smoke-') as temporary:
        path = Path(temporary)/'fixture.pdf'; path.write_bytes(pdf)
        info, _ = run([str(tools['pdfinfo']), str(path)])
        require(re.search(r'(?m)^Pages:\s+2$', info), 'PDF page count differs')
        require(re.search(r'(?m)^Encrypted:\s+no$', info), 'PDF encryption check differs')
        javascript, _ = run([str(tools['pdfinfo']), '-js', str(path)])
        require(not javascript.strip(), 'Fixture unexpectedly contains JavaScript')
        urls, _ = run([str(tools['pdfinfo']), '-url', str(path)])
        require(not any('http' in line for line in urls.splitlines()), 'Fixture unexpectedly contains links')
        attachments, _ = run([str(tools['pdfdetach']), '-list', str(path)])
        require(re.search(r'^0 embedded files', attachments), 'Fixture unexpectedly contains attachments')
        text, _ = run([str(tools['pdftotext']), '-layout', '-enc', 'UTF-8', str(path), '-'])
        pages = [page for page in text.split('\f') if page.strip()]
        require(len(pages) == 2, 'Extracted page boundaries differ')
        for actual, expected in zip(pages, TEXT):
            require(' '.join(actual.split()) == expected, 'Substantive PDF text changed during extraction')
    receipt.update(status='verified', fixture_sha256=hashlib.sha256(pdf).hexdigest(),
                   pages=2, substantive_characters=[len(p) for p in TEXT],
                   verified_commands=['pdfinfo', 'pdfinfo -js', 'pdfinfo -url', 'pdfdetach -list', 'pdftotext -layout -enc UTF-8'])
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', required=True, type=Path)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    receipt = verify(args.bin_dir)
    if args.receipt:
        args.receipt.write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
