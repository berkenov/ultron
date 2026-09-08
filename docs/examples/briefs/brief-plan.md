Роль: code (режим plan)
КОНТЕКСТ — учебный python-проект; функция greet должна здороваться словом hello. Режим планирования: изменения предлагаются патчем, дерево не трогается.
ФАЙЛЫ — читать: app/service.py, tests/test_service.py
ЗАДАЧА — подготовь unified diff для app/service.py: возвращаемая строка greet меняется с "hi {user.name}" на "hello {user.name}". Патч положи в поле patch. Файлы не менять.
НЕ ТРОГАТЬ — ничего не менять в дереве. Коммиты не создавать, git не трогать.
ПРИЁМКА — поле patch содержит корректный unified diff, применимый через git apply; changed_files пуст.
ОТВЕТ — Ответ строго по схеме. patch — unified diff с заголовками --- a/app/service.py и +++ b/app/service.py. changed_files пустой, commands пустой.
ОГРАНИЧЕНИЯ — язык ответа русский.
