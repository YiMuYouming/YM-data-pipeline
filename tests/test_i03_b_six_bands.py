import unittest, math, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"YM-data-pipeline"))
from ym_stock_data import indicators as i
class SixBandTests(unittest.TestCase):
 def test_b01_raw(self):
  for x,label in [(0,"冰点"),(19.999,"冰点"),(20,"弱修复"),(20.001,"弱修复"),(34.999,"弱修复"),(35,"低迷"),(35.001,"低迷"),(39.999,"低迷"),(40,"温和主升"),(40.001,"温和主升"),(59.999,"温和主升"),(60,"主升强势"),(60.001,"主升强势"),(79.999,"主升强势"),(80,"主升强势"),(80.001,"高潮"),(100,"高潮")]:
   with self.subTest(x=x): self.assertEqual(label,i.emotion_band(x))
 def test_b02_buffer(self):
  for lo,hi,label in [(19,21,"冰点"),(34,36,"弱修复"),(39,41,"低迷"),(59,61,"温和主升"),(79,81,"主升强势")]:
   for x in (lo,hi):
    with self.subTest(x=x):
     v=i.emotion_state(x);self.assertEqual(label,v["display_band"]);self.assertEqual(i.emotion_band(x),v["raw_band"])
   for x in (lo-.001,hi+.001):
    with self.subTest(outside=x): self.assertEqual(i.emotion_band(x),i.emotion_state(x)["display_band"])
 def test_b05_invalid(self):
  for x in (None,True,"40",[],{},float("nan"),float("inf"),-1,101):
   with self.subTest(x=repr(x)):
    self.assertIsNone(i.emotion_band(x));v=i.emotion_state(x);self.assertFalse(v["valid"]);self.assertIsNone(v["raw_band"]);self.assertIsNone(v["display_band"])
