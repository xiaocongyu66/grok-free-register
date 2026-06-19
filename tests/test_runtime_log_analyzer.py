import unittest

from runtime_log_analyzer import parse_monitor_lines, summarize_monitor_rows


class RuntimeLogAnalyzerTests(unittest.TestCase):
    def test_parses_csp_monitor_rows_and_recent_rates(self):
        text = "\n".join(
            [
                "[*] T:0 Q:3 phys:0 t_slot:3 q_slot:0 q_pend:0 "
                "t_prod:76 t_adm:76 t_exp:0 q_sent:87 q_ret:87 q_adm:79 q_exp:0 "
                "pair:76 ok:71 fail:0 rate:8.6/min #71",
                "[*] T:0 Q:3 phys:0 t_slot:3 q_slot:0 q_pend:0 "
                "t_prod:91 t_adm:91 t_exp:0 q_sent:101 q_ret:101 q_adm:94 q_exp:0 "
                "pair:91 ok:86 fail:0 rate:8.8/min #86",
            ]
        )

        rows = parse_monitor_lines(text)
        summary = summarize_monitor_rows(rows)

        self.assertEqual(rows[0].kind, "csp")
        self.assertEqual(summary["last_ok"], 86)
        self.assertAlmostEqual(summary["last_cumulative_rate"], 8.8)
        self.assertAlmostEqual(summary["recent_ok_per_min"], 9.89, places=2)
        self.assertAlmostEqual(summary["recent_t_prod_per_min"], 9.89, places=2)
        self.assertEqual(summary["last_q_minus_t"], 3)
        self.assertEqual(summary["last_q_return_minus_t_prod"], 10)

    def test_parses_state_machine_monitor_rows(self):
        text = "\n".join(
            [
                "[*] slots:7/8 act:7 cpu:83% avg:76%/85 mem:4841M "
                "T:4 Q:0 sent:0 got:0(0%) rate:0.0/min #0",
                "[*] slots:8/8 act:8 cpu:91% avg:88%/85 mem:5012M "
                "T:6 Q:5 sent:61 got:48(79%) rate:7.8/min #40",
                "[*] slots:6/8 act:6 cpu:100% avg:90%/85 mem:5351M "
                "T:1 Q:6 sent:68 got:68(100%) rate:8.8/min #59",
            ]
        )

        rows = parse_monitor_lines(text)
        summary = summarize_monitor_rows(rows)

        self.assertEqual(rows[-1].kind, "state_machine")
        self.assertEqual(summary["last_ok"], 59)
        self.assertAlmostEqual(summary["last_cumulative_rate"], 8.8)
        self.assertEqual(summary["last_q_minus_t"], 5)
        self.assertEqual(summary["last_slots"], 6)

    def test_parses_csp_v2_monitor_rows_with_admission_fields(self):
        text = (
            "[*] T:1 Q:2 phys:3 p_send:1 t_slot:4 q_slot:5 q_pend:6 "
            "p_batch:3.5 t_prog:1 q_inflight:4 "
            "t_prod:20 t_adm:18 t_exp:1 q_sent:24 q_ret:22 q_adm:20 q_exp:0 "
            "pair:17 ok:16 fail:1 rate:9.1/min #16"
        )

        rows = parse_monitor_lines(text)
        summary = summarize_monitor_rows(rows)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "csp")
        self.assertEqual(summary["last_ok"], 16)
        self.assertEqual(summary["last_q_return_minus_t_prod"], 2)


if __name__ == "__main__":
    unittest.main()
