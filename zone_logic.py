"""
Module ánh xạ tọa độ camera sang lưới LED Matrix 8x8.
"""

from __future__ import annotations


class Matrix8x8Controller:
    """
    Điều khiển vùng tối trên ma trận LED 8x8 bằng chuỗi lệnh `BOX:...`.
    """

    def __init__(self, matrix_width: int = 8, matrix_height: int = 8) -> None:
        if matrix_width <= 0 or matrix_height <= 0:
            raise ValueError("matrix dimensions must be > 0")

        self.matrix_width = int(matrix_width)
        self.matrix_height = int(matrix_height)

    def get_dark_box(
        self,
        smooth_x: float,
        smooth_y: float,
        smooth_z: float,
        screen_width: int | float,
        screen_height: int | float,
    ) -> str | None:
        """
        Ánh xạ tọa độ camera sang vùng tối trên matrix 8x8.

        Quy tắc:
        - Z > 100m: không cần vùng tối, trả về None.
        - Z < 30m: mở rộng ±2 ô.
        - 30m <= Z < 60m: mở rộng ±1 ô.
        - Z >= 60m: chỉ tối đúng một điểm.
        """
        if screen_width <= 0 or screen_height <= 0:
            raise ValueError("screen_width and screen_height must be > 0")

        if smooth_z > 100.0:
            return None

        safe_x = self._clamp_float(float(smooth_x), 0.0, float(screen_width))
        safe_y = self._clamp_float(float(smooth_y), 0.0, float(screen_height))

        # Chia đều toàn bộ ảnh thành 8 cột và 8 hàng.
        # Dùng * matrix_width thay vì * (matrix_width - 1) để mỗi ô có bề rộng đều nhau,
        # sau đó clamp lại biên cuối về 0..7.
        col_center = self._clamp(int((safe_x / float(screen_width)) * self.matrix_width), 0, self.matrix_width - 1)
        row_center = self._clamp(int((safe_y / float(screen_height)) * self.matrix_height), 0, self.matrix_height - 1)

        if smooth_z < 30.0:
            expand = 2
        elif smooth_z < 60.0:
            expand = 1
        else:
            expand = 0

        col_start = self._clamp(col_center - expand, 0, self.matrix_width - 1)
        col_end = self._clamp(col_center + expand, 0, self.matrix_width - 1)
        row_start = self._clamp(row_center - expand, 0, self.matrix_height - 1)
        row_end = self._clamp(row_center + expand, 0, self.matrix_height - 1)

        return f"BOX:{col_start}:{col_end}:{row_start}:{row_end}\n"

    @staticmethod
    def _clamp(value: int, minimum: int, maximum: int) -> int:
        """
        Giới hạn giá trị nguyên trong khoảng hợp lệ.
        """
        return max(minimum, min(value, maximum))

    @staticmethod
    def _clamp_float(value: float, minimum: float, maximum: float) -> float:
        """
        Giới hạn giá trị thực trước khi ánh xạ sang lưới.
        """
        return max(minimum, min(value, maximum))
