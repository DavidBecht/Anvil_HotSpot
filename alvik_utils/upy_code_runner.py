import sys
import _thread
from collections import deque
from alvik_logger.logger import logger
from alvik_utils.upy_streamwriter import UPYStreamWriter
from alvik_utils.utils import get_error_message
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio
import builtins

class MockSys:
    def __init__(self, stream):
        self._real_sys = sys
        self._real_print = builtins.print
        self.stdout = stream
        self.stderr = stream
        self.stream = stream
        self.version = self._real_sys.version

    def patch_sys(self):
        sys.modules['sys'] = self
        builtins.print = self.stream.print

    def undo_patch_sys(self):
        sys.modules['sys'] = self._real_sys
        builtins.print = self._real_print

    def print_exception(self, exc, file=None):
        # Fallback auf echte Implementation, aber mit umgeleitetem Output
        self._real_sys.print_exception(exc, file)

    def exit(self, retval = 0) -> None:
        self._real_sys.exit(retval)



class LiveStream:
    _exit_msg = "__EXIT__LIVESTREAM__"
    """Ersetzt `print()`, um Ausgabe live an den Client zu senden."""
    def __init__(self, writer: UPYStreamWriter):
        self._writer: UPYStreamWriter = writer
        self._msg_queue = deque((), 100)

    async def awrite(self, text: str) -> None:
        """Sendet jeden `print()`-Aufruf sofort weiter."""
        self.write(text)

    def write(self, text: str) -> None:
        """Sendet jeden `print()`-Aufruf sofort weiter."""
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8")
            except UnicodeDecodeError:
                text = repr(text)
        self._msg_queue.append(text)
    async def stream_writer_loop(self) -> None:
        exit = False
        while not exit:
            while self._msg_queue:
                try:
                    text = self._msg_queue.popleft()
                    if text == self._exit_msg:
                        exit = True
                        break
                    logger.info(text)
                    output = text.strip().replace("\n", "<br>")  # HTML-taugliche Zeilenumbrüche
                    await self._writer.awrite(f"data: {output}\n\n".encode("utf-8"))
                except ConnectionResetError:
                    logger.warning("Client hat die Verbindung getrennt.")
                    exit = True
                    break
            await asyncio.sleep(0.1)
        logger.info(f"Execution completed.")
        await self._writer.send_response(200)
        await self._writer.aclose()

    def print(self, *args) -> None:
        self.write(" ".join(map(str, args)))

    def flush(self) -> None:
        """Wird benötigt, damit `print()` korrekt funktioniert."""
        pass

    def close(self) -> None:
        self._msg_queue.append(self._exit_msg)


class UPYCodeRunner:
    def __init__(self, filename: str, writer: UPYStreamWriter):
        self._stream = LiveStream(writer)
        self._filename = filename
        self._should_stop = False
        self._is_running = False

    def stop(self):
        if self._is_running:
            self._is_running = False
            self._stream.write(f"Stopping execution of {self._filename}")
            self._should_stop = True

    def _stopped(self):
        self._should_stop = False
        self._is_running = False

    async def stop_and_wait(self):
        self.stop()
        while self._should_stop:
            self._stream.write(f"Waiting execution of file {self._filename} to be stopped")
            await asyncio.sleep(1)

    def _should_stop_signal(self):
        return self._should_stop

    def _run_code(self, code: str) -> None:
        mock_sys = MockSys(self._stream)
        mock_sys.patch_sys()

        namespace = {
            "print": self._stream.print,
            "sys" : MockSys(self._stream),
            "_should_stop_signal": self._should_stop_signal
        }
        try:
            exec(code, namespace)  # Code mit modifizierter `print()`-Funktion ausführen
        except Exception as e:
            error_trace = get_error_message(e)
            self._stream.write(f"ERROR:Execution of {self._filename} failed with {e}.\n{error_trace}")  # Fehler auch sofort senden
        finally:
            mock_sys.undo_patch_sys()
            self._stopped()
            self._stream.close()

    async def run_file(self) -> None:
        """Startet eine Python-Datei und sendet deren Output in Echtzeit zurück."""
        with open(self._filename, "r") as f:
            code = f.read()
        await self._stream.awrite(f"Running file {self._filename}")
        asyncio.create_task(self._stream.stream_writer_loop())
        self._is_running = True
        _thread.start_new_thread(self._run_code, (code, ))
