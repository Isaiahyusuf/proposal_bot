from app.templating import DEFAULT_TEMPLATE, render_message, validate_template


def test_all_variables_render():
    out = render_message("{project_name} {token_symbol} {blockchain} {developer_username} {project_website}",
                         {"project_name": "A", "token_symbol": "B", "blockchain": "sol",
                          "developer_username": "@d", "project_website": "https://x"})
    assert out == "A B sol @d https://x"


def test_missing_variables_leave_no_placeholders():
    out = render_message("Hi {developer_username}, about {project_name} ({token_symbol}) {project_website}.", {"project_name": "Foo"})
    assert "{" not in out and "}" not in out and "()" not in out
    assert out == "Hi, about Foo."


def test_default_template_without_data_is_clean():
    out = render_message(DEFAULT_TEMPLATE, {})
    assert "{" not in out and "your project on DexScreener" in out


def test_unknown_variable_rejected_on_save_and_stripped_on_render():
    assert "foo" in validate_template("hi {foo}")
    assert render_message("hi {foo}there", {}) == "hi there"


def test_values_are_not_re_expanded():
    assert render_message("{project_name}", {"project_name": "{token_symbol}", "token_symbol": "X"}) == "{token_symbol}"
