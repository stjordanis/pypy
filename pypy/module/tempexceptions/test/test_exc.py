
from pypy.conftest import gettestobjspace

class AppTestExc(object):
    def setup_class(cls):
        cls.space = gettestobjspace(usemodules=('tempexceptions',))

    def test_baseexc(self):
        from tempexceptions import BaseException

        assert str(BaseException()) == ''
        assert repr(BaseException()) == 'BaseException()'
        assert BaseException().message == ''
        assert BaseException(3).message == 3
        assert repr(BaseException(3)) == 'BaseException(3,)'
        assert str(BaseException(3)) == '3'
        assert BaseException().args == ()
        assert BaseException(3).args == (3,)
        assert BaseException(3, "x").args == (3, "x")
        assert repr(BaseException(3, "x")) == "BaseException(3, 'x')"
        assert str(BaseException(3, "x")) == "(3, 'x')"
        assert BaseException(3, "x").message == ''
        x = BaseException()
        x.xyz = 3
        assert x.xyz == 3

    def test_exc(self):
        from tempexceptions import Exception, BaseException

        assert issubclass(Exception, BaseException)
        assert isinstance(Exception(), Exception)
        assert isinstance(Exception(), BaseException)
        assert repr(Exception(3, "x")) == "Exception(3, 'x')"

    def test_custom_class(self):
        from tempexceptions import Exception

        class MyException(Exception):
            def __init__(self, x):
                self.x = x

            def __str__(self):
                return self.x

        assert issubclass(MyException, Exception)
        assert str(MyException("x")) == "x"

    def test_unicode_translate_error(self):
        from tempexceptions import UnicodeTranslateError
        ut = UnicodeTranslateError(u"x", 1, 5, "bah")
        assert ut.object == u'x'
        assert ut.start == 1
        assert ut.end == 5
        assert ut.reason == 'bah'
        assert ut.args == (u'x', 1, 5, 'bah')
        assert ut.message == ''
        ut.object = u'y'
        assert ut.object == u'y'
        assert str(ut) == "can't translate characters in position 1-4: bah"
        ut.start = 4
        ut.object = u'012345'
        assert str(ut) == "can't translate character u'\\x34' in position 4: bah"

    def test_key_error(self):
        from tempexceptions import KeyError

        assert str(KeyError('s')) == "'s'"

    def test_environment_error(self):
        from tempexceptions import EnvironmentError
        ee = EnvironmentError(3, "x", "y")
        assert str(ee) == "[Errno 3] x: y"
        assert str(EnvironmentError(3, "x")) == "[Errno 3] x"

    def test_syntax_error(self):
        from tempexceptions import SyntaxError
        s = SyntaxError(3)
        assert str(s) == '3'
        assert str(SyntaxError("a", "b", 123)) == "a"
        assert str(SyntaxError("a", (1, 2, 3, 4))) == "a (line 2)"
        s = SyntaxError("a", (1, 2, 3, 4))
        assert s.msg == "a"
        assert s.filename == 1
        assert str(SyntaxError("msg", ("file.py", 2, 3, 4))) == "msg (file.py, line 2)"

    def test_system_exit(self):
        from tempexceptions import SystemExit
        assert SystemExit().code is None
        assert SystemExit("x").code == "x"
        assert SystemExit(1, 2).code == (1, 2)

    def test_unicode_decode_error(self):
        from tempexceptions import UnicodeDecodeError
        ud = UnicodeDecodeError("x", "y", 1, 5, "bah")
        assert ud.encoding == 'x'
        assert ud.object == 'y'
        assert ud.start == 1
        assert ud.end == 5
        assert ud.reason == 'bah'
        assert ud.args == ('x', 'y', 1, 5, 'bah')
        assert ud.message == ''
        ud.object = 'z9'
        assert ud.object == 'z9'
        assert str(ud) == "'x' codec can't decode bytes in position 1-4: bah"
        ud.end = 2
        assert str(ud) == "'x' codec can't decode byte 0x39 in position 1: bah"

    def test_unicode_encode_error(self):
        from tempexceptions import UnicodeEncodeError
        ue = UnicodeEncodeError("x", u"y", 1, 5, "bah")
        assert ue.encoding == 'x'
        assert ue.object == u'y'
        assert ue.start == 1
        assert ue.end == 5
        assert ue.reason == 'bah'
        assert ue.args == ('x', u'y', 1, 5, 'bah')
        assert ue.message == ''
        ue.object = u'z9'
        assert ue.object == u'z9'
        assert str(ue) == "'x' codec can't encode characters in position 1-4: bah"
        ue.end = 2
        assert str(ue) == "'x' codec can't encode character u'\\x39' in position 1: bah"
