#pragma once

#include <cpp-httplib/httplib.h>

#include <cstdlib>

struct common_http_url {
    std::string scheme;
    std::string user;
    std::string password;
    std::string host;
    std::string path;
};

static common_http_url common_http_parse_url(const std::string & url);

static std::string common_http_get_env(const char * key_upper, const char * key_lower) {
    const char * val = std::getenv(key_upper);
    if (val && val[0] != '\0') {
        return val;
    }

    val = std::getenv(key_lower);
    if (val && val[0] != '\0') {
        return val;
    }

    return {};
}

static bool common_http_parse_host_port(const std::string & input, std::string & host, int & port) {
    if (input.empty()) {
        return false;
    }

    std::string host_port = input;
    size_t      host_begin = 0;

    if (host_port[0] == '[') {
        size_t host_end = host_port.find(']');
        if (host_end == std::string::npos || host_end + 1 >= host_port.size() || host_port[host_end + 1] != ':') {
            return false;
        }
        host = host_port.substr(1, host_end - 1);
        host_begin = host_end + 2;
    } else {
        size_t colon_pos = host_port.rfind(':');
        if (colon_pos == std::string::npos || colon_pos == 0 || colon_pos + 1 >= host_port.size()) {
            return false;
        }
        host = host_port.substr(0, colon_pos);
        host_begin = colon_pos + 1;
    }

    const std::string port_str = host_port.substr(host_begin);
    try {
        size_t consumed = 0;
        int parsed_port = std::stoi(port_str, &consumed);
        if (consumed != port_str.size() || parsed_port <= 0 || parsed_port > 65535) {
            return false;
        }
        port = parsed_port;
    } catch (...) {
        return false;
    }

    return !host.empty();
}

static bool common_http_parse_proxy_env(const std::string & proxy_env,
                                        std::string & proxy_host,
                                        int & proxy_port,
                                        std::string & proxy_user,
                                        std::string & proxy_password) {
    if (proxy_env.empty()) {
        return false;
    }

    std::string proxy_url = proxy_env;
    if (proxy_url.find("://") == std::string::npos) {
        proxy_url = "http://" + proxy_url;
    }

    common_http_url proxy_parts;
    try {
        proxy_parts = common_http_parse_url(proxy_url);
    } catch (...) {
        return false;
    }

    if (!common_http_parse_host_port(proxy_parts.host, proxy_host, proxy_port)) {
        return false;
    }

    proxy_user = proxy_parts.user;
    proxy_password = proxy_parts.password;
    return true;
}

static common_http_url common_http_parse_url(const std::string & url) {
    common_http_url parts;
    auto scheme_end = url.find("://");

    if (scheme_end == std::string::npos) {
        throw std::runtime_error("invalid URL: no scheme");
    }
    parts.scheme = url.substr(0, scheme_end);

    if (parts.scheme != "http" && parts.scheme != "https") {
        throw std::runtime_error("unsupported URL scheme: " + parts.scheme);
    }

    auto rest = url.substr(scheme_end + 3);
    auto at_pos = rest.find('@');

    if (at_pos != std::string::npos) {
        auto auth = rest.substr(0, at_pos);
        auto colon_pos = auth.find(':');
        if (colon_pos != std::string::npos) {
            parts.user = auth.substr(0, colon_pos);
            parts.password = auth.substr(colon_pos + 1);
        } else {
            parts.user = auth;
        }
        rest = rest.substr(at_pos + 1);
    }

    auto slash_pos = rest.find('/');

    if (slash_pos != std::string::npos) {
        parts.host = rest.substr(0, slash_pos);
        parts.path = rest.substr(slash_pos);
    } else {
        parts.host = rest;
        parts.path = "/";
    }
    return parts;
}

static std::pair<httplib::Client, common_http_url> common_http_client(const std::string & url) {
    common_http_url parts = common_http_parse_url(url);

    if (parts.host.empty()) {
        throw std::runtime_error("error: invalid URL format");
    }

#ifndef CPPHTTPLIB_OPENSSL_SUPPORT
    if (parts.scheme == "https") {
        throw std::runtime_error(
            "HTTPS is not supported. Please rebuild with one of:\n"
            "  -DLLAMA_BUILD_BORINGSSL=ON\n"
            "  -DLLAMA_BUILD_LIBRESSL=ON\n"
            "  -DLLAMA_OPENSSL=ON (default, requires OpenSSL dev files installed)"
        );
    }
#endif

    httplib::Client cli(parts.scheme + "://" + parts.host);

    std::string proxy_env;
    if (parts.scheme == "https") {
        proxy_env = common_http_get_env("HTTPS_PROXY", "https_proxy");
        if (proxy_env.empty()) {
            proxy_env = common_http_get_env("HTTP_PROXY", "http_proxy");
        }
    } else {
        proxy_env = common_http_get_env("HTTP_PROXY", "http_proxy");
    }

    std::string proxy_host;
    int         proxy_port = -1;
    std::string proxy_user;
    std::string proxy_password;
    if (common_http_parse_proxy_env(proxy_env, proxy_host, proxy_port, proxy_user, proxy_password)) {
        cli.set_proxy(proxy_host, proxy_port);
        if (!proxy_user.empty()) {
            cli.set_proxy_basic_auth(proxy_user, proxy_password);
        }
    }

    if (!parts.user.empty()) {
        cli.set_basic_auth(parts.user, parts.password);
    }

    cli.set_follow_location(true);

    return { std::move(cli), std::move(parts) };
}

static std::string common_http_show_masked_url(const common_http_url & parts) {
    return parts.scheme + "://" + (parts.user.empty() ? "" : "****:****@") + parts.host + parts.path;
}
