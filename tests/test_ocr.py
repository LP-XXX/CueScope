from __future__ import annotations

import unittest

import numpy as np

from deal_watch.config import ModelConfig
from deal_watch.models import FrameSample, Rect
from deal_watch.ocr import ImageBlockFilter, LocalOcr


class ImageFilterTests(unittest.TestCase):
    def test_colored_seed_excludes_entire_product_image_panel(self):
        image = np.full((400, 500, 3), 250, dtype=np.uint8)
        image[20:260, 20:220] = 255
        image[24:144, 24:144] = np.random.default_rng(8).integers(
            0, 256, size=(120, 120, 3), dtype=np.uint8
        )
        # 白底商品图片中，彩色种子以外的黑字同样应被遮罩。
        image[190:210, 50:190] = 0
        image[300:350, 20:300] = 237  # 独立的普通聊天气泡
        boxes = ImageBlockFilter().find(image)
        self.assertEqual(len(boxes), 1)
        panel = boxes[0]
        self.assertLessEqual(panel.y, 20)
        self.assertGreaterEqual(panel.y + panel.height, 260)
        self.assertGreaterEqual(panel.x + panel.width, 220)
        masked = ImageBlockFilter.mask(image, boxes)
        self.assertTrue(np.all(masked[190:210, 50:190] == 250))
        self.assertTrue(np.all(masked[300:350, 20:300] == 237))

    def test_plain_chat_background_is_not_masked(self):
        image = np.full((300, 500, 3), 250, dtype=np.uint8)
        image[30:180, 15:440] = 237
        for y in range(50, 170, 30):
            image[y:y + 5, 30:400:2] = 0
        self.assertEqual(ImageBlockFilter().find(image), [])

    def test_large_chat_bubble_and_frame_border_do_not_mask_entire_roi(self):
        image = np.full((600, 500, 3), 250, dtype=np.uint8)
        image[240:590, 10:480] = 237
        image[0, :] = 235
        image[:, -1] = 235
        image[20:210, 20:220] = 255
        image[24:144, 24:144] = np.random.default_rng(8).integers(
            0, 256, size=(120, 120, 3), dtype=np.uint8
        )
        boxes = ImageBlockFilter().find(image)
        masked = ImageBlockFilter.mask(image, boxes)
        self.assertTrue(boxes)
        self.assertTrue(np.array_equal(masked[260:580, 30:450], image[260:580, 30:450]))


class UncoveredAvatarTests(unittest.TestCase):
    """预览页头像型小块不画红框，真正的卡片区域仍然计入未覆盖。"""

    def test_avatar_sized_box_is_not_reported_as_uncovered(self):
        image = np.full((400, 500, 3), 250, dtype=np.uint8)
        avatar = Rect(10, 20, 60, 60)
        card = Rect(180, 150, 200, 140)
        ocr = LocalOcr(ModelConfig(), base_dir=None)
        # 不加载真实模型：这里只验证未覆盖矩形的剔除逻辑。
        ocr._engine = lambda _prepared: None
        ocr.filter.find = lambda _image, background=None: [avatar, card]
        observation = ocr.recognize(
            FrameSample("uncovered", 1, 0.0, image, Rect(0, 0, 500, 400))
        )
        self.assertEqual(observation.blocks, [])
        self.assertNotIn(avatar, observation.uncovered_boxes)
        self.assertIn(card, observation.uncovered_boxes)


if __name__ == "__main__":
    unittest.main()
