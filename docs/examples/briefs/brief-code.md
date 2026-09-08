Роль: code
КОНТЕКСТ — учебный python-проект; функция greet должна здороваться словом hello.
ФАЙЛЫ — читать и менять: app/service.py; читать: tests/test_service.py
ЗАДАЧА — в app/service.py замени возвращаемую строку функции greet с "hi {user.name}" на "hello {user.name}". Затем прогони гейт: python3 tests/test_service.py — он должен пройти с кодом 0.
НЕ ТРОГАТЬ — app/models.py, tests/, README.md. Коммиты не создавать, git не трогать.
ПРИЁМКА — python3 tests/test_service.py завершается с кодом 0.
ОТВЕТ — Ответ строго по схеме. changed_files — каждый тронутый файл. commands — гейт и итог одной фразой, без вывода. patch оставить пустым.
ОГРАНИЧЕНИЯ — язык ответа русский; стиль кода не менять; новых зависимостей не добавлять.
