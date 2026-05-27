import unittest

from main import chunk_message, parse_highlights


SAMPLE_HTML = """
<html>
  <body>
    <section>
      <h2>Highlights</h2>
      <div>
        <h3>Tech</h3>
        <ul>
          <li>Nvidia (NVDA) rises 3% after AI chip demand update.</li>
          <li>Apple announces a June 10 developer event.</li>
        </ul>
      </div>
      <div>
        <h3>Healthcare</h3>
        <ul>
          <li>Pfizer reports FDA review date for a new therapy.</li>
        </ul>
      </div>
      <div>
        <h3>Energy &amp; Utilities</h3>
        <ul>
          <li>Oil prices move higher as supply concerns return.</li>
        </ul>
      </div>
    </section>
  </body>
</html>
"""


class ParserTest(unittest.TestCase):
    def test_parse_sector_highlights_from_mock_html(self):
        highlights = parse_highlights(SAMPLE_HTML)

        self.assertIn("科技 Tech", highlights)
        self.assertIn("医疗 Healthcare", highlights)
        self.assertIn("能源与公用事业 Energy & Utilities", highlights)
        self.assertEqual(len(highlights["科技 Tech"]), 2)
        self.assertIn("Nvidia (NVDA)", highlights["科技 Tech"][0])

    def test_chunk_message_respects_limit(self):
        chunks = chunk_message("a\n" + ("b" * 20), limit=10)

        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))
        self.assertGreater(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
