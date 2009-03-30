import xmlrpclib
import traceback
import re
from cStringIO import StringIO

allowed = ('package_releases', 'package_urls', 'package_data',
    'search', 'list_packages', 'release_urls', 'release_data',
    'updated_releases', 'changelog', 'post_cheesecake_for_release')

# monkey-patch xmlrpclib to marshal mx.DateTime correctly.
import mx.DateTime
def dump_DateTime(self, value, write):
    write("<value><dateTime.iso8601>")
    write(value.strftime("%Y%m%dT%H:%M:%S"))
    write("</dateTime.iso8601></value>\n")
xmlrpclib.Marshaller.dispatch[mx.DateTime.DateTimeType] = dump_DateTime

def handle_request(webui_obj):
    webui_obj.handler.send_response(200, 'OK')
    webui_obj.handler.send_header('Content-type', 'text/xml')
    webui_obj.handler.send_header('charset', 'UTF-8' );
    webui_obj.handler.end_headers()
    try:
        methodArgs, methodName = xmlrpclib.loads(webui_obj.handler.rfile.read())
        if methodName in allowed:
            response = globals()[methodName](webui_obj.store, *methodArgs)
        else:
            raise KeyError, "Method %r does not exist" % (methodName,)
        if response is None:
            response = ''
        # xmlrpclib.dumps encodes Unicode as UTF-8
        xml = xmlrpclib.dumps((response,), methodresponse=True, allow_none=True)
        xml = re.sub('([\x00-\x08]|[\x0b-\x0c]|[\x0e-\x1f])+', '', xml)
        webui_obj.handler.wfile.write(xml)
    except:
        out = StringIO()
        traceback.print_exc(file=out)
        result = xmlrpclib.dumps(xmlrpclib.Fault(1, out.getvalue()), methodresponse=True)
        webui_obj.handler.wfile.write(result)

def list_packages(store):
    result = store.get_packages()
    return [row['name'] for row in result]

def package_releases(store, package_name, show_hidden=False):
    if show_hidden:
        hidden = None
    else:
        hidden = False
    result = store.get_package_releases(package_name, hidden=hidden)
    return [row['version'] for row in result]

def release_urls(store, package_name, version):
    result = []
    for file in store.list_files(package_name, version):
        info = file.as_dict()
        info['url'] = store.gen_file_url(info['python_version'],
            package_name, info['filename'])
        result.append(info)
    # TODO do something with release_urls when there is something to do
    #info = store.get_package(package_name, version)
    #if info['download_url']:
    #    result.append({'url': info['download_url']})
    return result
package_urls = release_urls     # "deprecated"


def release_data(store, package_name, version):
    info = store.get_package(package_name, version).as_dict()
    del info['description_html']
    for col in ('requires', 'provides', 'obsoletes'):
        rows = store.get_release_relationships(package_name, version, col)
        info[col] = [row['specifier'] for row in rows]
    classifiers = [r[0] for r in store.get_release_classifiers(package_name,
        version)]
    info['classifiers' ] = classifiers
    return info
package_data = release_data     # "deprecated"

def search(store, spec, operator='and'):
    spec['_pypi_hidden'] = 'FALSE'
    return [row.as_dict() for row in store.query_packages(spec, operator)]

def updated_releases(store, since):
    result = store.updated_releases(since)
    return [(row['name'], row['version']) for row in result]

def changelog(store, since):
    result = store.changelog(since)
    return [(row['name'],row['version'],
             int(row['submitted_date'].gmticks()),
             row['action'])
             for row in result]

def post_cheesecake_for_release(store, name, version, score_data, password):
    if password != store.config.cheesecake_password:
        raise ValuError("Bad password.")

    store.save_cheesecake_score(name, version, score_data)
    store.commit()
