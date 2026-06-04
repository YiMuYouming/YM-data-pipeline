"""Tests for Tonghuashun industry parsing."""

import unittest

from ym_stock_data.sources.ths_industry import _parse_industry_rows


class ThsIndustryTests(unittest.TestCase):
    def test_parse_industry_rows_extracts_881_code_and_flow(self):
        html = """
        <table>
          <tr>
            <td>30</td>
            <td><a href="http://q.10jqka.com.cn/thshy/detail/code/881124/" target="_blank">消费电子</a></td>
            <td class="c-fall">-0.89</td>
            <td>1362.27</td>
            <td>531.42</td>
            <td>-14.12</td>
            <td class="c-rise">25</td>
            <td class="c-fall">70</td>
            <td>39.00</td>
            <td><a href="http://stockpage.10jqka.com.cn/300868/" target="_blank">杰美特</a></td>
            <td class="c-rise">125.29</td>
            <td class="c-rise">9.53</td>
          </tr>
        </table>
        """

        rows = _parse_industry_rows(html)

        self.assertEqual(rows, [{
            "code": "881124",
            "name": "消费电子",
            "change_pct": -0.89,
            "latest": 1362.27,
            "turnover_yi": 531.42,
            "net_inflow_yi": -14.12,
            "main_net_inflow_yi": -14.12,
            "up_count": 25,
            "down_count": 70,
            "flat_count": 39,
            "leader": "杰美特",
            "leader_price": 125.29,
            "leader_change_pct": 9.53,
        }])


if __name__ == "__main__":
    unittest.main()
