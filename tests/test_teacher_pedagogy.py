"""Different requests need different teaching operations and evidence boundaries."""
import unittest

import tests  # Establish synthetic-only storage before application imports.
from services.teacher_pedagogy import applicable_rules, chemistry_question, teaching_plan


class TeacherPedagogyTests(unittest.TestCase):
    def test_student_error_is_corrected_at_the_mistaken_judgment(self):
        plan = teaching_plan("我覺得 CH3Br 可以消去，因為帶Br的碳上有氫。我的判斷哪裡錯？")
        self.assertEqual(plan["route"], "correction")
        self.assertEqual(teaching_plan("CH3Br不能消去，因為分子裡沒有氫，對嗎？")["route"], "correction")

    def test_solvent_missing_and_experimental_interference_are_distinct(self):
        self.assertEqual(teaching_plan("溴乙烷加NaOH一定生成乙醇嗎？先判斷甚麼？")["route"], "conditions")
        self.assertEqual(teaching_plan("水解後為甚麼不能直接滴AgNO3，要用稀硝酸酸化？")["route"], "experiment")

    def test_gas_quantity_checks_apply_to_attached_liter_units(self):
        self.assertEqual(teaching_plan("5.6L乙炔，求消耗溴的質量。")["route"], "calculation")

    def test_explicit_answers_override_every_teaching_template(self):
        plan = teaching_plan("只填答案，不要解釋：①加成還是取代？②用硝酸還是鹽酸？")
        self.assertTrue(plan["answer_only"])
        self.assertEqual(applicable_rules([{"id": "rule", "routes": []}], plan), [])
        self.assertFalse(teaching_plan("不要只給定義，教我怎樣判斷化學平衡。")["answer_only"])
        self.assertTrue(teaching_plan("只要最終答案：乙烯是甚麼烴？")["answer_only"])

    def test_followup_and_experiment_quantity_do_not_use_other_task_examples(self):
        self.assertEqual(teaching_plan("還是不懂羥基換在哪。")["route"], "followup")
        self.assertEqual(teaching_plan("取1 g溴乙烷水解後，為甚麼要加稀硝酸？")["route"], "experiment")
        self.assertEqual(teaching_plan("1 mol乙烯與Br2反應，實驗怎麼檢驗產物？")["route"], "experiment")

    def test_rules_follow_task_scope_and_priority_instead_of_all_rules(self):
        rules = [
            {"id": "micro-question-then-answer", "routes": ["concept", "experiment"]},
            {"id": "condition-purpose", "routes": ["experiment"]},
            {"id": "finish-with-writeable-answer", "routes": ["concept", "experiment"]},
            {"id": "equation-before-calculation", "routes": ["calculation"]}]
        selected = applicable_rules(rules, teaching_plan("實驗中為什麼要酸化？"))
        self.assertEqual([rule["id"] for rule in selected], ["condition-purpose", "micro-question-then-answer", "finish-with-writeable-answer"])

    def test_stable_chemistry_method_does_not_require_a_known_chapter(self):
        self.assertTrue(chemistry_question("合成氨反應提高壓強，化學平衡怎樣移動？"))
        self.assertFalse(chemistry_question("宇宙紅移是甚麼？"))

    def test_organic_local_rewrite_is_not_applied_to_equilibrium(self):
        plan = teaching_plan("化學平衡移動怎麼判斷？")
        rules = [{"id": "local-structure-edit", "routes": ["concept"], "topics": ["organic"]},
                 {"id": "micro-question-then-answer", "routes": ["concept"]}]
        self.assertEqual([rule["id"] for rule in applicable_rules(rules, plan)], ["micro-question-then-answer"])


if __name__ == "__main__":
    unittest.main()
