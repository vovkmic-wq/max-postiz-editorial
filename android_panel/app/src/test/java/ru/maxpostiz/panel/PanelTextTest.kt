package ru.maxpostiz.panel

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PanelTextTest {
    @Test
    fun serverPhasesUseRussianLabels() {
        assertEquals("Ожидание времени публикации", status("waiting_dispatch"))
        assertEquals("Подготовка статьи", status("preparing"))
        assertEquals("Резерв пополнен", status("reserve_refilled"))
        assertEquals("В расписании", status("QUEUE"))
        assertEquals("Опубликовано", status("PUBLISHED"))
        assertEquals("Исход неизвестен", status("blocked_unknown"))
    }

    @Test
    fun newServerCodesHaveRussianFallbacks() {
        assertNull(status(null))
        assertNull(status(" "))
        assertEquals("Состояние не распознано", status("future_state"))
        assertEquals("Сервер сообщил об ограничении. Обновите данные.", blocker("FUTURE_ERROR"))
        assertEquals("Шлюз отклонил запрос.", blocker("FUTURE_ERROR", "Шлюз отклонил запрос."))
        assertEquals("Postiz не подтвердил изменение. Требуется сверка.", blocker("POSTIZ_UNKNOWN"))
    }

    @Test
    fun environmentAndTargetsHaveReadableNames() {
        assertEquals("Рабочий сервер", environmentName("vm112-live", fixture = false))
        assertEquals("Копия базы · только просмотр", environmentName("vm112-db-copy-readonly", fixture = false))
        assertEquals("Тестовый стенд", environmentName("fixture", fixture = true))
        assertEquals("Подключённый сервер", environmentName("future_env", fixture = false))
        assertEquals("MAX · тестовый канал", targetName("max_test"))
        assertEquals("Пикабу · основной профиль", targetName("pikabu_main"))
        assertEquals("Пикабу · нейроизображения", targetName("pikabu_neuro_images"))
        assertEquals("ВКонтакте", targetName("new_vk_target", "vk"))
        assertEquals("Адресат", targetName("future_target", "future_platform"))
    }
}
