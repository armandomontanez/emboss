# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Adds auto-generated virtual fields to the IR."""

from front_end import attributes
from public import ir_pb2
from util import expression_parser
from util import ir_util
from util import traverse_ir


def _mark_as_synthetic(proto):
  """Marks all source_locations in proto with is_synthetic=True."""
  if not isinstance(proto, ir_pb2.Message):
    return
  if hasattr(proto, "source_location"):
    proto.source_location.is_synthetic = True
  for name, value in proto.raw_fields.items():
    if name != "source_location":
      if isinstance(value, ir_pb2.TypedScopedList):
        for i in range(len(value)):
          _mark_as_synthetic(value[i])
      else:
        _mark_as_synthetic(value)


def _skip_text_output_attribute():
  """Returns the IR for a [text_output: "Skip"] attribute."""
  result = ir_pb2.Attribute(
      name=ir_pb2.Word(text=attributes.TEXT_OUTPUT),
      value=ir_pb2.AttributeValue(string_constant=ir_pb2.String(text="Skip")))
  _mark_as_synthetic(result)
  return result


# The existence condition for an alias for an anonymous bits' field is the union
# of the existence condition for the anonymous bits and the existence condition
# for the field within.  The 'x' and 'x.y' are placeholders here; they'll be
# overwritten in _add_anonymous_aliases.
_ANONYMOUS_BITS_ALIAS_EXISTENCE_SKELETON = expression_parser.parse(
    "$present(x) && $present(x.y)")


def _add_anonymous_aliases(structure, type_definition):
  """Adds synthetic alias fields for all fields in anonymous fields.

  This essentially completes the rewrite of this:

      struct Foo:
        0  [+4]  bits:
          0  [+1]  Flag  low
          31 [+1]  Flag  high

  Into this:

      struct Foo:
        bits EmbossReservedAnonymous0:
          [text_output: "Skip"]
          0  [+1]  Flag  low
          31 [+1]  Flag  high
        0 [+4]  EmbossReservedAnonymous0  emboss_reserved_anonymous_1
        let low = emboss_reserved_anonymous_1.low
        let high = emboss_reserved_anonymous_1.high

  Note that this pass runs very, very early -- even before symbols have been
  resolved -- so very little in ir_util will work at this point.

  Arguments:
      structure: The ir_pb2.Structure on which to synthesize fields.
      type_definition: The ir_pb2.TypeDefinition containing structure.

  Returns:
      None
  """
  new_fields = []
  for field in structure.field:
    new_fields.append(field)
    if not field.name.is_anonymous:
      continue
    field.attribute.extend([_skip_text_output_attribute()])
    for subtype in type_definition.subtype:
      if (subtype.name.name.text ==
          field.type.atomic_type.reference.source_name[-1].text):
        field_type = subtype
        break
    else:
      assert False, ("Unable to find corresponding type {} for anonymous field "
                     "in {}.".format(
                         field.type.atomic_type.reference, type_definition))
    anonymous_reference = ir_pb2.Reference(source_name=[field.name.name])
    anonymous_field_reference = ir_pb2.FieldReference(
        path=[anonymous_reference])
    for subfield in field_type.structure.field:
      alias_field_reference = ir_pb2.FieldReference(
          path=[
              anonymous_reference,
              ir_pb2.Reference(source_name=[subfield.name.name]),
          ]
      )
      new_existence_condition = ir_pb2.Expression()
      new_existence_condition.CopyFrom(_ANONYMOUS_BITS_ALIAS_EXISTENCE_SKELETON)
      existence_clauses = new_existence_condition.function.args
      existence_clauses[0].function.args[0].field_reference.CopyFrom(
          anonymous_field_reference)
      existence_clauses[1].function.args[0].field_reference.CopyFrom(
          alias_field_reference)
      new_read_transform = ir_pb2.Expression(
          field_reference=alias_field_reference)
      # This treats *most* of the alias field as synthetic, but not its name(s):
      # leaving the name(s) as "real" means that symbol collisions with the
      # surrounding structure will be properly reported to the user.
      _mark_as_synthetic(new_existence_condition)
      _mark_as_synthetic(new_read_transform)
      new_alias = ir_pb2.Field(
          read_transform=new_read_transform,
          existence_condition=new_existence_condition,
          name=subfield.name)
      if subfield.HasField("abbreviation"):
        new_alias.abbreviation.CopyFrom(subfield.abbreviation)
      _mark_as_synthetic(new_alias.existence_condition)
      _mark_as_synthetic(new_alias.read_transform)
      new_fields.append(new_alias)
      # Since the alias field's name(s) are "real," it is important to mark the
      # original field's name(s) as synthetic, to avoid duplicate error
      # messages.
      _mark_as_synthetic(subfield.name)
      if subfield.HasField("abbreviation"):
        _mark_as_synthetic(subfield.abbreviation)
  del structure.field[:]
  structure.field.extend(new_fields)


_SIZE_BOUNDS = {
    "$max_size_in_bits": expression_parser.parse("$upper_bound($size_in_bits)"),
    "$min_size_in_bits": expression_parser.parse("$lower_bound($size_in_bits)"),
    "$max_size_in_bytes": expression_parser.parse(
        "$upper_bound($size_in_bytes)"),
    "$min_size_in_bytes": expression_parser.parse(
        "$lower_bound($size_in_bytes)"),
}


def _add_size_bound_virtuals(structure, type_definition):
  """Adds ${min,max}_size_in_{bits,bytes} virtual fields to structure."""
  names = {
      ir_pb2.TypeDefinition.BIT: ("$max_size_in_bits", "$min_size_in_bits"),
      ir_pb2.TypeDefinition.BYTE: ("$max_size_in_bytes", "$min_size_in_bytes"),
  }
  for name in names[type_definition.addressable_unit]:
    bound_field = ir_pb2.Field(
        read_transform=_SIZE_BOUNDS[name],
        name=ir_pb2.NameDefinition(name=ir_pb2.Word(text=name)),
        existence_condition=expression_parser.parse("true"),
        attribute=[_skip_text_output_attribute()]
    )
    _mark_as_synthetic(bound_field.read_transform)
    structure.field.extend([bound_field])


# Each non-virtual field in a structure generates a clause that is passed to
# `$max()` in the definition of `$size_in_bits`/`$size_in_bytes`.  Additionally,
# the `$max()` call is seeded with a `0` argument: this ensures that
# `$size_in_units` is never negative, and ensures that structures with no
# physical fields don't end up with a zero-argument `$max()` call, which would
# fail type checking.
_SIZE_CLAUSE_SKELETON = expression_parser.parse(
    "existence_condition ? start + size : 0")
_SIZE_SKELETON = expression_parser.parse("$max(0)")


def _add_size_virtuals(structure, type_definition):
  """Adds a $size_in_bits or $size_in_bytes virtual field to structure."""
  names = {
      ir_pb2.TypeDefinition.BIT: "$size_in_bits",
      ir_pb2.TypeDefinition.BYTE: "$size_in_bytes",
  }
  size_field_name = names[type_definition.addressable_unit]
  size_clauses = []
  for field in structure.field:
    # Virtual fields do not have a physical location, and thus do not contribute
    # to the size of the structure.
    if ir_util.field_is_virtual(field):
      continue
    size_clause = ir_pb2.Expression()
    size_clause.CopyFrom(_SIZE_CLAUSE_SKELETON)
    # Copy the appropriate clauses into `existence_condition ? start + size : 0`
    size_clause.function.args[0].CopyFrom(field.existence_condition)
    size_clause.function.args[1].function.args[0].CopyFrom(field.location.start)
    size_clause.function.args[1].function.args[1].CopyFrom(field.location.size)
    size_clauses.append(size_clause)
  size_expression = ir_pb2.Expression()
  size_expression.CopyFrom(_SIZE_SKELETON)
  size_expression.function.args.extend(size_clauses)
  _mark_as_synthetic(size_expression)
  size_field = ir_pb2.Field(
      read_transform=size_expression,
      name=ir_pb2.NameDefinition(name=ir_pb2.Word(text=size_field_name)),
      existence_condition=ir_pb2.Expression(
          boolean_constant=ir_pb2.BooleanConstant(value=True)
      ),
      attribute=[_skip_text_output_attribute()]
  )
  structure.field.extend([size_field])


def _add_virtuals_to_structure(structure, type_definition):
  _add_anonymous_aliases(structure, type_definition)
  _add_size_virtuals(structure, type_definition)
  _add_size_bound_virtuals(structure, type_definition)


def synthesize_fields(ir):
  """Adds synthetic fields to all structures.

  Adds aliases for all fields in anonymous `bits` to the enclosing structure.

  Arguments:
      ir: The IR to which to add fields.

  Returns:
      A list of errors, or an empty list.
  """
  traverse_ir.fast_traverse_ir_top_down(
      ir, [ir_pb2.Structure], _add_virtuals_to_structure)
  return []
