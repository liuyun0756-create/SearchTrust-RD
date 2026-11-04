"""A bounded structural prefix ends after a complete HTML tag, never mid-attribute."""
from html.parser import HTMLParser


def complete_tag_prefix(text: str) -> str:
    class BoundaryParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.end = 0
            self.lines = [0]
            self.lines.extend(i + 1 for i, c in enumerate(text) if c == "\n")

        def source_offset(self):
            line, column = self.getpos()
            return self.lines[line - 1] + column

        def handle_starttag(self, tag, attrs):
            self.end = self.source_offset() + len(self.get_starttag_text())

        def handle_endtag(self, tag):
            self.end = text.index(">", self.source_offset()) + 1

    parser = BoundaryParser()
    # Do not close(): EOF recovery may reinterpret an unfinished tag as text.
    parser.feed(text)
    return text[:parser.end]
