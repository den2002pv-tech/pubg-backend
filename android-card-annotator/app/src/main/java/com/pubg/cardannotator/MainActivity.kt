package com.pubg.cardannotator

import androidx.activity.ComponentActivity
import android.content.Intent
import android.graphics.*
import android.net.Uri
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import org.json.JSONArray
import org.json.JSONObject
import org.opencv.android.OpenCVLoader
import org.opencv.android.Utils
import org.opencv.core.Mat
import org.opencv.core.MatOfPoint
import org.opencv.core.MatOfPoint2f
import org.opencv.core.Point
import org.opencv.core.Size
import org.opencv.imgproc.Imgproc
import kotlin.math.max
import kotlin.math.min

class MainActivity : ComponentActivity() {
    private lateinit var imageView: BoundaryView
    private lateinit var status: TextView
    private var bitmap: Bitmap? = null
    private var manual = false
    private val picker = registerForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri != null) loadImage(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildScreen()
    }

    private fun buildScreen() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setBackgroundColor(Color.rgb(15, 23, 42))
        }
        root.addView(TextView(this).apply {
            text = "PUBG · Разметка границ карт"
            textSize = 21f; setTextColor(Color.WHITE)
            setTypeface(null, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "Выберите скриншот. Автопоиск отметит возможные карточки; проверьте рамки и исправьте вручную."
            textSize = 13f; setTextColor(Color.LTGRAY); setPadding(0, dp(6), 0, dp(10))
        })
        val top = LinearLayout(this)
        top.addView(button("Выбрать скриншот") { picker.launch("image/*") }, weight())
        top.addView(button("Найти границы") { detect() }, weight())
        root.addView(top)
        imageView = BoundaryView()
        root.addView(imageView, LinearLayout.LayoutParams(-1, 0, 1f).apply { topMargin = dp(8); bottomMargin = dp(8) })
        status = TextView(this).apply {
            text = "Сначала выберите скриншот."
            textSize = 13f; setTextColor(Color.WHITE); setPadding(0, dp(3), 0, dp(6))
        }
        root.addView(status)
        val bottom = LinearLayout(this)
        bottom.addView(button("Ручная рамка: выкл.") {
            manual = !manual; imageView.manual = manual
            (it as Button).text = if (manual) "Ручная рамка: вкл." else "Ручная рамка: выкл."
            status.text = if (manual) "Проведите пальцем по диагонали карточки." else "Ручной режим выключен."
        }, weight())
        bottom.addView(button("Отменить последнюю") {
            if (imageView.boxes.isNotEmpty()) imageView.boxes.removeAt(imageView.boxes.lastIndex)
            imageView.invalidate(); updateStatus()
        }, weight())
        root.addView(bottom)
        root.addView(button("Экспортировать JSON") { exportJson() }, LinearLayout.LayoutParams(-1, dp(48)).apply { topMargin = dp(6) })
        setContentView(root)
    }

    private fun dp(n: Int) = (n * resources.displayMetrics.density).toInt()
    private fun weight() = LinearLayout.LayoutParams(0, dp(46), 1f).apply { marginEnd = dp(4) }
    private fun button(label: String, action: (View) -> Unit) = Button(this).apply {
        text = label; textSize = 12f; isAllCaps = false
        setTextColor(Color.WHITE); setBackgroundColor(Color.rgb(51, 65, 85))
        setOnClickListener(action); minHeight = dp(42); minimumHeight = dp(42)
    }

    private fun loadImage(uri: Uri) {
        try {
            val loaded = contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it) }
                ?: error("Не удалось прочитать изображение")
            val factor = min(1f, 1800f / max(loaded.width, loaded.height))
            bitmap = if (factor < 1f) Bitmap.createScaledBitmap(loaded,
                (loaded.width * factor).toInt().coerceAtLeast(1),
                (loaded.height * factor).toInt().coerceAtLeast(1), true) else loaded
            imageView.setBitmap(bitmap!!)
            status.text = "Изображение ${bitmap!!.width}×${bitmap!!.height}. Нажмите «Найти границы»."
        } catch (e: Exception) {
            Toast.makeText(this, "Не удалось открыть изображение: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun detect() {
        val source = bitmap ?: run {
            Toast.makeText(this, "Сначала выберите скриншот.", Toast.LENGTH_SHORT).show(); return
        }
        if (!OpenCVLoader.initLocal()) {
            status.text = "Не удалось инициализировать OpenCV."; return
        }
        try {
            val rgba = Mat(); Utils.bitmapToMat(source, rgba)
            val gray = Mat(); Imgproc.cvtColor(rgba, gray, Imgproc.COLOR_RGBA2GRAY)
            Imgproc.GaussianBlur(gray, gray, Size(5.0, 5.0), 0.0)
            val edges = Mat(); Imgproc.Canny(gray, edges, 45.0, 140.0)
            val kernel = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, Size(5.0, 5.0))
            Imgproc.morphologyEx(edges, edges, Imgproc.MORPH_CLOSE, kernel, Point(-1.0, -1.0), 2)
            val contours = ArrayList<MatOfPoint>()
            Imgproc.findContours(edges, contours, Mat(), Imgproc.RETR_LIST, Imgproc.CHAIN_APPROX_SIMPLE)
            val candidates = mutableListOf<RectF>()
            val totalArea = source.width.toDouble() * source.height
            for (contour in contours) {
                val r = Imgproc.boundingRect(contour)
                val w = r.width.toDouble(); val h = r.height.toDouble()
                if (w < source.width * 0.045 || w > source.width * 0.8) continue
                if (h < source.height * 0.10 || h > source.height * 0.98) continue
                if (w / max(1.0, h) !in 0.40..1.05 || w * h < totalArea * 0.004) continue
                val curve = MatOfPoint2f(*contour.toArray().map { Point(it.x, it.y) }.toTypedArray())
                val poly = MatOfPoint2f()
                Imgproc.approxPolyDP(curve, poly, Imgproc.arcLength(curve, true) * 0.025, true)
                val valid = poly.total() in 4L..8L
                curve.release(); poly.release()
                if (valid) candidates.add(RectF(r.x.toFloat(), r.y.toFloat(),
                    (r.x + r.width).toFloat(), (r.y + r.height).toFloat()))
            }
            val result = mutableListOf<RectF>()
            candidates.sortedByDescending { it.width() * it.height() }.forEach { candidate ->
                if (result.none { overlap(it, candidate) > 0.72f }) result.add(candidate)
            }
            val boxes = result.filter { it.width() < source.width * 0.75f && it.height() < source.height * 0.92f }
                .sortedWith(compareBy<RectF> { it.top }.thenBy { it.left })
            imageView.setBoxes(boxes)
            status.text = if (boxes.isEmpty()) "Автопоиск ничего не нашёл. Включите ручную рамку."
                else "Кандидатов: ${boxes.size}. Рамки предварительные — проверьте их."
            rgba.release(); gray.release(); edges.release(); kernel.release()
            contours.forEach { it.release() }
        } catch (e: Exception) {
            status.text = "Ошибка анализа: ${e.message ?: "неизвестная ошибка"}"
        }
    }

    private fun overlap(a: RectF, b: RectF): Float {
        val intersection = RectF(a)
        if (!intersection.intersect(b)) return 0f
        return intersection.width() * intersection.height() /
            min(a.width() * a.height(), b.width() * b.height()).coerceAtLeast(1f)
    }

    private fun updateStatus() { status.text = "Рамок: ${imageView.boxes.size}. Проверьте их перед экспортом." }

    private fun exportJson() {
        val source = bitmap
        if (source == null || imageView.boxes.isEmpty()) {
            Toast.makeText(this, "Нет рамок для экспорта.", Toast.LENGTH_SHORT).show(); return
        }
        val cards = JSONArray()
        imageView.boxes.forEachIndexed { i, r ->
            cards.put(JSONObject().put("index", i + 1).put("x", r.left.toInt()).put("y", r.top.toInt())
                .put("width", r.width().toInt()).put("height", r.height().toInt())
                .put("right", r.right.toInt()).put("bottom", r.bottom.toInt()))
        }
        val json = JSONObject().put("format", "pubg-card-boundaries-v1")
            .put("image_width", source.width).put("image_height", source.height)
            .put("coordinate_system", "pixels_from_top_left_of_resized_image")
            .put("review_required", true).put("cards", cards).toString(2)
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "application/json"; putExtra(Intent.EXTRA_SUBJECT, "PUBG card boundaries")
            putExtra(Intent.EXTRA_TEXT, json)
        }
        startActivity(Intent.createChooser(send, "Экспорт разметки"))
    }

    private inner class BoundaryView : View(this@MainActivity) {
        private val imagePaint = Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG)
        private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE; strokeWidth = dp(2).toFloat(); color = Color.rgb(34, 197, 94)
        }
        private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = dp(13).toFloat() }
        private val shade = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0x99000000.toInt() }
        private var source: Bitmap? = null
        val boxes = mutableListOf<RectF>()
        var manual = false
        private var startX = 0f; private var startY = 0f
        private var draft: RectF? = null
        private var imageRect = RectF()

        fun setBitmap(value: Bitmap) { source = value; boxes.clear(); invalidate() }
        fun setBoxes(values: List<RectF>) { boxes.clear(); boxes.addAll(values.map(::RectF)); invalidate() }

        override fun onDraw(canvas: Canvas) {
            super.onDraw(canvas)
            val img = source ?: run {
                label.color = Color.LTGRAY
                canvas.drawText("Предварительный просмотр скриншота", dp(12).toFloat(), height / 2f, label)
                return
            }
            val scale = min(width.toFloat() / img.width, height.toFloat() / img.height)
            val left = (width - img.width * scale) / 2f
            val top = (height - img.height * scale) / 2f
            imageRect = RectF(left, top, left + img.width * scale, top + img.height * scale)
            canvas.drawBitmap(img, null, imageRect, imagePaint)
            boxes.forEachIndexed { i, r ->
                val mapped = map(r, scale, left, top)
                canvas.drawRect(mapped, border)
                canvas.drawRect(mapped.left, mapped.top, mapped.left + dp(27), mapped.top + dp(19), shade)
                canvas.drawText("${i + 1}", mapped.left + dp(4), mapped.top + dp(14), label)
            }
            draft?.let { canvas.drawRect(it, border) }
        }

        override fun onTouchEvent(event: MotionEvent): Boolean {
            if (!manual || source == null) return false
            when (event.action) {
                MotionEvent.ACTION_DOWN -> { startX = event.x; startY = event.y; draft = RectF(startX, startY, startX, startY); invalidate(); return true }
                MotionEvent.ACTION_MOVE -> { draft = RectF(min(startX, event.x), min(startY, event.y), max(startX, event.x), max(startY, event.y)); invalidate(); return true }
                MotionEvent.ACTION_UP -> {
                    val img = source ?: return true
                    val scale = min(width.toFloat() / img.width, height.toFloat() / img.height)
                    val left = (width - img.width * scale) / 2f; val top = (height - img.height * scale) / 2f
                    val screen = RectF(draft ?: return true)
                    if (!screen.intersect(imageRect) || screen.width() < dp(18) || screen.height() < dp(18)) {
                        draft = null; invalidate(); return true
                    }
                    boxes.add(RectF(((screen.left-left)/scale).coerceIn(0f,img.width.toFloat()),
                        ((screen.top-top)/scale).coerceIn(0f,img.height.toFloat()),
                        ((screen.right-left)/scale).coerceIn(0f,img.width.toFloat()),
                        ((screen.bottom-top)/scale).coerceIn(0f,img.height.toFloat())))
                    boxes.sortWith(compareBy<RectF> { it.top }.thenBy { it.left })
                    draft = null; invalidate(); updateStatus(); return true
                }
            }
            return true
        }

        private fun map(r: RectF, scale: Float, left: Float, top: Float) =
            RectF(left+r.left*scale, top+r.top*scale, left+r.right*scale, top+r.bottom*scale)
    }
}
