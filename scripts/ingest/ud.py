import argparse
import re
import time

from typing import Optional

from decaf.index import Literal, Structure, DecafIndex

from conllu import TokenList, TokenTree, Token, parse_incr


#
# UD constants
#

# metadata to be carried over across sentences
# format: 'regex' -> 'index field' / None (keep as is)
METADATA_CARRYOVER = {
	r'^newdoc( id)?': 'document',  # indicates start of new document (optionally with ID)
	r'^newpar( id)?': 'paragraph',  # indicates start of new document (optionally with ID)
	r'meta::.+': None  # GUM document-level metadata field (e.g., 'meta::dateCollected')
}


#
# helper functions
#

def parse_arguments():
	parser = argparse.ArgumentParser(description="UD Importer")
	parser.add_argument('--input', required=True, help='path to UD treebank in CoNLL-U format')
	parser.add_argument('--output', required=True, help='path to output SQLite index')
	parser.add_argument('--literal-level', default='character', help='level at which to store atomic literals (default: character)')
	return parser.parse_args()


def get_carryover_field(field):
	for carryover_pattern, index_field in METADATA_CARRYOVER.items():
		# check if metadata field matches carryover field pattern
		if re.match(carryover_pattern, field) is None:
			continue
		# check if field requires name conversion
		if index_field is None:
			return field
		else:
			return index_field
	# return None is examined field is not for carryover
	return None


#
# parser functions
#

def parse_token(token:Token, cursor_idx:int, literal_level:str, trailing_space:Optional[bool] = None) -> tuple[list[Literal], list[Structure], list[tuple[Structure, Structure]]]:
	literals, structures, hierarchies = [], [], []

	# create literals from characters
	if literal_level == 'character':
		for character_idx, character in enumerate(token['form']):
			literals.append(
				Literal(start=cursor_idx + character_idx, end=cursor_idx + character_idx + 1, value=character)
			)
	# create literals from tokens
	elif literal_level == 'token':
		literals.append(Literal(start=cursor_idx, end=cursor_idx + len(token['form']), value=token['form']))
	else:
		raise ValueError(f"Unknown literal level: '{literal_level}'.")

	trailing_space = True if trailing_space is None else trailing_space

	# create structures from UD's token-level annotations
	# https://universaldependencies.org/format.html
	start_idx, end_idx = cursor_idx, cursor_idx + len(token['form'])
	# token's surface form
	token_structure = Structure(
		start=start_idx, end=end_idx,
		value=None, stype='token',  # value=None as it's constructed from its literals
		literals=[l for l in literals]
	)
	structures.append(token_structure)
	# create structures from other token-level annotations
	for annotation in token:
		# skip redundant annotations (UD ID, dep-tuple)
		if annotation in {'id', 'deps', 'form'}:
			continue
		# skip empty annotation fields
		elif token[annotation] is None:
			continue
		# split multi-value annotation fields into individual structures
		elif type(token[annotation]) is dict:
			for misc_annotation, misc_value in token[annotation].items():
				structures.append(
					Structure(
						start=start_idx, end=end_idx,
						value=misc_value, stype=misc_annotation,
						literals=[l for l in literals]
					)
				)
				hierarchies.append((token_structure, structures[-1]))
			# check for SpaceAfter=No MISC annotation
			if ('SpaceAfter' in token[annotation]) and (token[annotation]['SpaceAfter'] == 'No'):
				# prevent adding trailing space
				trailing_space = False
		# all other annotations are stored as token-level structures
		else:
			structures.append(
				Structure(
					start=start_idx, end=end_idx,
					value=token[annotation], stype=annotation,
					literals=[l for l in literals]
				)
			)
			hierarchies.append((token_structure, structures[-1]))

	if trailing_space:
		literals.append(Literal(start=end_idx, end=end_idx + 1, value=' '))

	return literals, structures, hierarchies


def parse_dependencies(tree:TokenTree, token_structures:dict[int, list[Structure]]):
	structures, hierarchies = [], []

	relation = tree.token['deprel']
	token_id = tree.token['id']
	literals = token_structures[token_id][0].literals
	start_idx, end_idx = token_structures[token_id][0].start, token_structures[token_id][0].end

	# recursively process child nodes
	for child in tree.children:
		child_structures, child_hierarchies = parse_dependencies(tree=child, token_structures=token_structures)
		structures += child_structures
		hierarchies += child_hierarchies
		literals += token_structures[child.token['id']][0].literals
		start_idx = min(start_idx, token_structures[child.token['id']][0].start)
		end_idx = max(end_idx, token_structures[child.token['id']][0].end)

	# append parent structure
	dependency = Structure(
		start=start_idx, end=end_idx,
		value=relation, stype='dependency',
		literals=literals
	)
	hierarchies += \
		[(dependency, child) for child in structures] + \
		[(dependency, grandchild) for _, grandchild in hierarchies] + \
		[(dependency, child) for child in token_structures[token_id]]
	structures.append(dependency)

	return structures, hierarchies


def parse_sentence(sentence:TokenList, cursor_idx:int, literal_level:str) -> tuple[list[Literal], list[Structure], list[tuple[Structure,Structure]], dict]:
	literals, structures, hierarchies = [], [], []
	carryover = {}

	# parse tokens in sentence
	token_cursor_idx = int(cursor_idx)
	token_structures_by_id = {}
	multitoken_end = None
	multitoken_space = None
	for token_idx, token in enumerate(sentence):
		# check for multi-tokens (e.g. "It's" -> "It 's"), identified by ID with range (e.g., '3-4')
		if type(token['id']) is tuple:
			multitoken_end = token['id'][-1]
			if (token['misc'] is not None) and ('SpaceAfter' in token['misc']) and (token['misc']['SpaceAfter'] == 'No'):
				multitoken_space = False
			continue
		# trailing space behaviour follows default, except within and at the end of multi-tokens
		trailing_space = None
		if multitoken_end is not None:
			trailing_space = False
			if token['id'] >= multitoken_end:
				trailing_space = multitoken_space
				multitoken_end, multitoken_space = None, None

		# process token
		token_literals, token_structures, token_hierarchies = parse_token(
			token, token_cursor_idx,
			literal_level=literal_level,
			trailing_space=trailing_space
		)
		literals += token_literals
		structures += token_structures
		hierarchies += token_hierarchies
		token_structures_by_id[token['id']] = token_structures
		token_cursor_idx += len(token_literals)

	# create hierarchical dependency structures
	dependency_structures, dependency_hierarchies = parse_dependencies(
		tree=sentence.to_tree(),
		token_structures=token_structures_by_id
	)
	structures += dependency_structures
	hierarchies += dependency_hierarchies

	# create structures from UD's sentence-level annotations
	start_idx, end_idx = cursor_idx, token_cursor_idx
	# sentence structure itself
	sentence_structure = Structure(
		start=start_idx, end=end_idx,
		value=None, stype='sentence',
		literals=[l for l in literals]
	)
	sentence_structures = [sentence_structure]
	# sentence metadata
	for meta_field, meta_value in sentence.metadata.items():
		# extract special carryover metadata ('newdoc id', 'newpar id', 'newpar', ...)
		carryover_field = get_carryover_field(meta_field)
		if carryover_field is not None:
			carryover[carryover_field] = (meta_value, start_idx)
			continue
		# skip redundant UD field (text)
		if meta_field == 'text':
			continue
		# all other metadata are stored as sentence-level structures
		sentence_structures.append(
			Structure(
				start=start_idx, end=end_idx,
				value=meta_value, stype=meta_field,
				literals=[l for l in literals]
			)
		)

	# establish sentence-level hierarchies
	hierarchies += \
			[(sentence_structure, child) for child in structures] + \
			[(sentence_structure, grandchild) for _, grandchild in hierarchies] + \
			[(sentence_structure, sibling) for sibling in sentence_structures[1:]]

	structures += sentence_structures
	hierarchies = list(set(hierarchies))  # remove redundant hierarchies (e.g., from sentence and dependency level)

	return literals, structures, hierarchies, carryover


def parse_carryover(
		carryover:dict, next_carryover:dict,
		literals:dict[str, list[Literal]], next_literals:list[Literal],
		carryover_hierarchies:dict[str, list[tuple[Structure,Structure]]], next_hierarchies:list[tuple[Structure,Structure]],
		cursor_idx:int
) -> tuple[dict, list[Structure], dict[str, list[Literal]], dict[str, list[tuple[Structure,Structure]]], list[tuple[Structure,Structure]]]:
	structures = []
	output_hierarchies = []

	# check if paragraph (or document) changed
	if ('paragraph' in next_carryover) or ('document' in next_carryover):
		paragraph_id, paragraph_start_idx = carryover['paragraph']
		structures.append(
			Structure(
				start=paragraph_start_idx, end=cursor_idx,
				value=None, stype='paragraph',
				literals=literals['paragraph']
			)
		)

		if paragraph_id:
			structures.append(
				Structure(
					start=paragraph_start_idx, end=cursor_idx,
					value=paragraph_id, stype='paragraph_id',
					literals=literals['paragraph']
				)
			)

		# add hierarchical structures at parameter-level
		for co_structure in structures:
			output_hierarchies += [(co_structure, child) for _, child in carryover_hierarchies['paragraph']]

		# reset parameter-level carryover
		next_carryover['paragraph'] = next_carryover.get('paragraph', (None, cursor_idx))
		literals['paragraph'] = []
		carryover_hierarchies['paragraph'] = []

	# check if document changed
	if 'document' in next_carryover:
		document = None
		document_structures = []
		# create document-level structures and flush metadata
		for co_field, (co_value, co_start) in carryover.items():
			# create separate document and document ID structures
			if co_field == 'document':
				structures.append(
					Structure(
						start=co_start, end=cursor_idx,
						value=None, stype='document',
						literals=literals['document']
					)
				)
				co_field = 'document_id'
				document = structures[-1]

			# skip re-processing of paragraph metadata
			if co_field == 'paragraph':
				continue

			# add remaining document-level metadata
			structures.append(
				Structure(
					start=co_start, end=cursor_idx,
					value=co_value, stype=co_field,
					literals=literals['document']
				)
			)
			document_structures.append(structures[-1])

		# add document-level hierarchical structures
		if document is not None:
			output_hierarchies += [(document, child) for _, child in carryover_hierarchies['document']]
			output_hierarchies += [(document, child) for child in document_structures]

		# reset all carryover data
		carryover = next_carryover
		literals = {s:[] for s in literals}
		carryover_hierarchies = {s:[] for s in carryover_hierarchies}

	literals = {s: v + next_literals for s, v in literals.items()}
	carryover_hierarchies = {s: v + next_hierarchies for s, v in carryover_hierarchies.items()}
	output_hierarchies = list(set(output_hierarchies))  # remove redundant hierarchies

	return carryover, structures, literals, carryover_hierarchies, output_hierarchies


#
# main
#

def main():
	args = parse_arguments()
	print("="*13)
	print("📥️ UD Import")
	print("="*13)

	# set up associated DECAF index
	decaf_index = DecafIndex(db_path=args.output)
	print(f"Connected to DECAF index at '{args.output}'.")

	print(f"Loading UD treebank from '{args.input}'...")
	with open(args.input) as fp, decaf_index as di:
		num_literals, num_structures, num_hierarchies = di.get_size()
		cursor_idx = 0  # initialize character-level dataset cursor
		carryover = {}  # initialize cross-sentence carryover metadata (e.g., document/paragraph info)
		carryover_literals = {s:[] for s in METADATA_CARRYOVER.values() if s is not None}  # initialize cross-sentence carryover literals for paragraphs and documents
		carryover_hierarchies = {s:[] for s in METADATA_CARRYOVER.values() if s is not None}  # initialize cross-sentence carryover hierarchies

		# iterate over sentences
		start_time = time.time()
		for sentence_idx, sentence in enumerate(parse_incr(fp)):
			print(f"\x1b[1K\r[{sentence_idx + 1}] Building index...", end='', flush=True)
			cur_literals, cur_structures, cur_hierarchies, cur_carryover = parse_sentence(
				sentence, cursor_idx,
				literal_level=args.literal_level
			)

			# process carryover metadata
			if sentence_idx == 0:
				carryover = cur_carryover  # first carryover metadata is always retained
				carryover_literals = {s:cur_literals for s in carryover_literals}
				carryover_hierarchies = {s:cur_hierarchies for s in carryover_hierarchies}
			else:
				carryover, new_structures, carryover_literals, carryover_hierarchies, new_hierarchies  = parse_carryover(
					carryover, cur_carryover,
					carryover_literals, cur_literals,
					carryover_hierarchies, cur_hierarchies,
					cursor_idx
				)
				cur_structures += new_structures
				cur_hierarchies += new_hierarchies

			# insert sentence-level literals into index (this updates the associated literals' index IDs)
			di.add_literals(literals=cur_literals)
			# insert sentence-level structures into index (associate structures and previously initialized literal IDs)
			di.add_structures(structures=cur_structures)
			# insert sentence-level hierarchies into index
			di.add_hierarchies(hierarchies=cur_hierarchies)

			cursor_idx += len(cur_literals)  # increment character-level cursor by number of atoms (i.e., characters)

		# process final carryover structures
		_, new_structures, _, _, new_hierarchies = parse_carryover(
			carryover, {'document': ('end', -1), 'paragraph': ('end', -1)},
			carryover_literals, [],
			carryover_hierarchies, [],
			cursor_idx
		)
		di.add_structures(structures=new_structures)
		di.add_hierarchies(hierarchies=new_hierarchies)

		# compute number of added structures
		new_num_literals, new_num_structures, new_num_hierarchies = di.get_size()
		print(
			f"\x1b[1K\rBuilt index with {new_num_literals - num_literals} literals "
			f"and {new_num_structures - num_structures} structures "
			f"with {new_num_hierarchies - num_hierarchies} hierarchical relations "
			f"for {sentence_idx + 1} sentences "
			f"from '{args.input}'"
			f"in {time.time() - start_time:.2f}s.")

		# print statistics
		literal_counts = di.get_literal_counts()
		print(f"Literal Statistics ({sum(literal_counts.values())} total; {len(literal_counts)} unique):")
		for atom, count in sorted(literal_counts.items(), key=lambda i: i[1], reverse=True):
			print(f"  '{atom}': {count} occurrences")

		structure_counts = di.get_structure_counts()
		print(f"Structure Statistics ({sum(structure_counts.values())} total; {len(structure_counts)} unique):")
		for structure, count in sorted(structure_counts.items(), key=lambda i: i[1], reverse=True):
			print(f"  '{structure}': {count} occurrences")

	print(f"Saved updated DECAF index to '{args.output}'.")


if __name__ == '__main__':
	main()